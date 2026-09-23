"""sshtsf -- pick an ssh+tmux session by name, alias, or menu.

    sshtsf [-e|-E] [-w|-W] [--dry-run] [HOST [SESSION]]
    sshtsf -n HOST [NAME]
    sshtsf -l [HOST] | -L [HOST] | -c [HOST [SESSION]] | --edit

ssh to a host and `tmux new-session -A` there: it remembers the host, session
name, remote folder and command in ~/.config/sshtsf/config.toml so that a
two-word invocation replaces a hand-written alias per session.

    sshtsf                  pick a host, then a session
    sshtsf devbox           pick a session on devbox
    sshtsf devbox web       connect to session web on devbox
    sshtsf devweb           the same, via a host+session alias
    sshtsf -n devbox        register a new session on devbox, then connect
    sshtsf -n devbox api    ...with api proposed as the name
    sshtsf -e devbox web    ...forwarding the local Emacs socket
    sshtsf -w devbox web    ...forwarding Wayland, for GUI applications

    sshtsf -l [HOST]        show the registered hosts and sessions
    sshtsf -L [HOST]        show the live tmux sessions on the remote(s)
    sshtsf -c               configure: pick a host to edit, or add one
    sshtsf -c devbox        ...its settings, or one of its sessions
    sshtsf -c devbox web    ...that session; an unknown name registers it
    sshtsf --edit           open the config in $VISUAL / $EDITOR (vi if neither)

Selections use fzf when it is on PATH and fall back to a numbered menu.
-c registers a host or session that does not exist yet through the same
prompts it edits one with: each field shows its current value, blank keeps
it and `-` clears it, or, at the name prompt, removes the entry. Connecting
registers an unknown session on a known host the same way, but refuses an
unknown host, pointing at `sshtsf -c HOST`. A word typed on its own -- a
host's name or alias, a session's alias -- names one entry, and the prompts
refuse one that is taken. Hosts to
add are offered from the ssh-lists file xssh and pssh read (see
`ssh-logins -h`), then from ~/.ssh/known_hosts; a name typed is fine too.
The same two lists are shown when a name does not resolve.

With ecf on, the connection also reverse-forwards the local Emacs server
socket, so emacsclient / $EDITOR / magit on the remote open in the local
Emacs, provided the remote's EDITOR is `emacsclient-auto` (this package; see
README). The tmux session is also told the destination as dialed, as
EMACS_REMOTE_TARGET, for the remote's TRAMP prefix. It is remembered per
host or per session; -e / -E override for one call. If the remote runs
SELinux (Fedora, Bazzite) and sshd is blocked from creating Unix sockets,
give the host a TCP port to relay through (asked by -c when ecf is on; an
`ecf_port` on a session, set by hand, wins over the host's).

With waypipe on, the connection is wrapped in `waypipe ssh`, so applications
started inside the remote tmux session draw on the local Wayland desktop.
Remembered the same way; -w / -W override. It needs a Wayland session here
and waypipe installed at both ends -- on the remote, on the PATH a
non-interactive ssh gets. Extra waypipe options (--compress, --no-gpu,
--xwls, --remote-bin, ...) go in SSHTSF_WAYPIPE_OPTS. The two forwards are
independent and compose. A waypipe session names the terminal tab
HOST:SESSION itself, since the terminal only sees waypipe, not the ssh
behind it; the name stays on the tab after the session ends.

A missing tool is a warning, never a refusal. A forward whose tool is absent
at either end (no Emacs server, no waypipe, no socat on the remote) is left
out and said so on stderr; a remote without tmux, or a session that cannot
be created or attached, gets a plain login shell in the session's folder
instead. The worst case is a plain ssh, not a connection that dies.

Everything sshtsf runs on the remote (tmux, socat) is looked for in the
Homebrew, Linuxbrew and ~/.local/bin directories first, since a non-
interactive ssh does not read the rc file that adds them. SSHTSF_REMOTE_PATH,
colon-separated, replaces that list.

The session is told waypipe's display name on connect, so no tmux config is
needed on the remote -- WAYLAND_DISPLAY only joined tmux's default
update-environment in 3.7. Panes that predate the connection still hold the
old value; a shell prompt hook that re-reads `tmux show-environment' keeps
those current.

Config layout (all fields but the host key are optional):

    default_host = "devbox"

    [last]
    host = "devbox"
    session = "web"

    [hosts.devbox]
    target = "devbox"       # ssh destination, [user@]host; defaults to the host key
    alias = "c"
    ecf = true              # forward the local Emacs socket, by default
    ecf_port = 41234        # relay via TCP port (for SELinux hosts where sshd cannot bind unix sockets)

    [hosts.devbox.sessions.web]
    alias = "devweb"        # host+session alias, usable as a single word
    folder = "src/webapp"
    command = "make dev"
    ecf = false             # ... except for this session
    waypipe = true          # ... and forward Wayland for this one
"""

from __future__ import annotations

import getpass
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
from typing import NamedTuple

import click
import tomllib

from . import lists, logins

CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
CONFIG_DIR = os.path.join(CONFIG_HOME, "sshtsf")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.toml")

NEW_HOST = "+ new host"
NEW_SESSION = "+ new session"

# Where an ecf forward lands on the remote, less the `-USER' the remote
# appends for the login it is made under, so two users on one host do not
# fight over a file in a sticky /tmp. Shared with the remote side: its
# `emacsclient-auto` looks under EMACSCLIENT_FORWARD_SOCKET, and the two agree
# on the default /tmp/emacs-remote-socket-USER.
ECF_REMOTE_SOCKET = os.environ.get("ECF_REMOTE_SOCKET") or "/tmp/emacs-remote-socket"

# Extra options spliced into the waypipe argv, e.g. --compress zstd, --no-gpu,
# --xwls, or --remote-bin for a host where waypipe is not on the PATH a
# non-interactive ssh gets. An env knob rather than config fields, so waypipe's
# whole option set is reachable without teaching this file any of it.
WAYPIPE_OPTS = shlex.split(os.environ.get("SSHTSF_WAYPIPE_OPTS") or "")

# Put ahead of PATH on the remote before anything is run there. A
# non-interactive `ssh host cmd' gets the login shell's bare PATH -- zsh reads
# only .zshenv for it, and Homebrew's shellenv line lives in .zprofile -- so
# a brew-installed tmux on a Mac is invisible to it. These are where package
# managers put what the panes' interactive shell finds, and they go first so
# the tmux the ssh command runs is the one the panes use (see live_sessions).
# $HOME is left for the remote sh to expand. A colon-separated
# SSHTSF_REMOTE_PATH replaces the list.
REMOTE_PATH = os.environ.get("SSHTSF_REMOTE_PATH") or ":".join([
    "/opt/homebrew/bin", "/usr/local/bin", "/home/linuxbrew/.linuxbrew/bin",
    "$HOME/.local/bin"])

# The boolean fields, as opposed to the string ones. Both resolve the same way
# (flag, then session, then host) and both are written as bare TOML literals.
BOOL_FIELDS = ("ecf", "waypipe")

# Accepted spellings for the boolean fields in the config.
TRUE_WORDS = ("true", "yes", "on", "1")
FALSE_WORDS = ("false", "no", "off", "0")

# Machines you have actually ssh'd to, offered as candidates when a name is
# not registered here. known_hosts rather than ~/.ssh/config because it
# accumulates on its own, with no upkeep.
KNOWN_HOSTS_PATH = os.path.expanduser("~/.ssh/known_hosts")

# Enough to jog a memory; a long-lived known_hosts runs to hundreds.
MAX_KNOWN_HOSTS = 24


# --------------------------------------------------------------------------
# tiny TOML writer
#
# tomllib reads but does not write, and tomli_w is not installed. The schema
# here is small and fully known, so emit it directly rather than take on a
# dependency. Comments in a hand-edited config are lost on rewrite; that is
# the one cost of this approach.
# --------------------------------------------------------------------------

BARE_KEY_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def toml_key(key: str) -> str:
    if key and all(c in BARE_KEY_OK for c in key):
        return key
    return toml_str(key)


def toml_str(value: str) -> str:
    out = value.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    return '"%s"' % out


def toml_bool(value) -> str:
    return "true" if value else "false"


def dump_config(cfg: dict) -> str:
    lines: list[str] = [
        "# sshtsf config -- see `sshtsf --help`.",
        "# Rewritten by sshtsf; comments outside this header are not preserved.",
        "",
    ]

    if cfg.get("default_host"):
        lines.append("default_host = %s" % toml_str(cfg["default_host"]))
        lines.append("")

    last = cfg.get("last") or {}
    if last.get("host"):
        lines.append("[last]")
        lines.append("host = %s" % toml_str(last["host"]))
        if last.get("session"):
            lines.append("session = %s" % toml_str(last["session"]))
        lines.append("")

    # The booleans are keyed on presence, not truth, unlike the string fields:
    # a session's `ecf = false` is what opts it out of a host default, so it
    # has to survive a rewrite rather than be dropped as falsy.
    for host, hcfg in sorted((cfg.get("hosts") or {}).items()):
        lines.append("[hosts.%s]" % toml_key(host))
        for field in ("target", "alias"):
            if hcfg.get(field):
                lines.append("%s = %s" % (field, toml_str(hcfg[field])))
        if hcfg.get("ecf_port"):
            lines.append("ecf_port = %s" % hcfg["ecf_port"])
        for field in BOOL_FIELDS:
            if field in hcfg:
                lines.append("%s = %s" % (field, toml_bool(hcfg[field])))
        lines.append("")

        for sess, scfg in sorted((hcfg.get("sessions") or {}).items()):
            lines.append("[hosts.%s.sessions.%s]" % (toml_key(host), toml_key(sess)))
            for field in ("alias", "folder", "command"):
                if scfg.get(field):
                    lines.append("%s = %s" % (field, toml_str(scfg[field])))
            if scfg.get("ecf_port"):
                lines.append("ecf_port = %s" % scfg["ecf_port"])
            for field in BOOL_FIELDS:
                if field in scfg:
                    lines.append("%s = %s" % (field, toml_bool(scfg[field])))
            lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


class ConfigError(Exception):
    """The config cannot be read; str() is the line to show, sans prefix."""


def read_config() -> dict:
    """The config as written, or ConfigError. A missing file is an empty config."""
    if not os.path.exists(CONFIG_PATH):
        return {"hosts": {}}
    try:
        with open(CONFIG_PATH, "rb") as fh:
            cfg = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("%s is not valid TOML: %s" % (CONFIG_PATH, exc)) from exc
    cfg.setdefault("hosts", {})
    return cfg


def load_config() -> dict:
    try:
        return read_config()
    except ConfigError as exc:
        sys.exit("sshtsf: %s" % exc)


def save_config(cfg: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    # Write via a temp file in the same dir, then rename: a crash or a full
    # disk leaves the previous config intact rather than a truncated one.
    fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".config.toml.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(dump_config(cfg))
        os.replace(tmp, CONFIG_PATH)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------------------
# selection helpers
# --------------------------------------------------------------------------


def have_fzf() -> bool:
    return shutil.which("fzf") is not None


def pick(items: list[str], prompt: str, header: str = "",
         free_text: bool = False, query: str = "") -> str | None:
    """Choose one of items. Returns None if the user aborted.

    free_text allows a value that is not in the list -- right for a remote
    folder, wrong for a host or session, where anything off-list is a typo and
    would only fail a lookup later. query pre-fills fzf's search box.
    """
    if not items:
        return None
    if have_fzf() and sys.stdin.isatty():
        cmd = ["fzf", "--prompt", prompt + " ", "--height", "60%", "--reverse"]
        if header:
            cmd += ["--header", header]
        if query:
            cmd += ["--query", query]
        if free_text:
            # print-query puts the typed text on line 1 and any match after it,
            # so a value with no match still comes back.
            cmd += ["--print-query"]
        proc = subprocess.run(cmd, input="\n".join(items), text=True,
                              stdout=subprocess.PIPE)
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        if free_text:
            # Prefer a real selection; fall back to the query when nothing
            # matched. Exit 130 is abort; exit 1 with a query is "no match".
            if proc.returncode == 130:
                return None
            return (lines[-1] if lines else None)
        if proc.returncode != 0:
            return None
        return lines[-1] if lines else None
    return pick_numbered(items, prompt, header, free_text)


MENU_LIMIT = 40


def pick_numbered(items: list[str], prompt: str, header: str = "",
                  free_text: bool = False) -> str | None:
    """Numbered fallback for when fzf is absent."""
    if header:
        print(header, file=sys.stderr)
    shown = items[:MENU_LIMIT]
    for i, item in enumerate(shown, 1):
        print("  %2d) %s" % (i, item), file=sys.stderr)
    if len(items) > len(shown):
        note = "type a value to use it" if free_text else "install fzf to filter"
        print("  ... %d more (%s)" % (len(items) - len(shown), note),
              file=sys.stderr)

    hint = "1-%d, a value, or q" % len(shown) if free_text \
        else "1-%d, or q" % len(shown)
    while True:
        try:
            reply = input("%s [%s]: " % (prompt, hint)).strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return None
        if reply in ("q", "Q", ""):
            return None
        if reply.isdigit() and 1 <= int(reply) <= len(shown):
            return shown[int(reply) - 1]
        if reply in items:
            return reply
        if free_text:
            return reply
        print("  not a choice: %s" % reply, file=sys.stderr)


def said_yes(reply: str) -> bool:
    """Whether a (y/N) answer was a yes; blank is the default no."""
    return reply.strip().lower() in TRUE_WORDS + ("y",)


def ask(prompt: str, default: str = "") -> str:
    suffix = " [%s]" % default if default else ""
    try:
        reply = input("%s%s: " % (prompt, suffix)).strip()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        raise SystemExit(130)
    return reply or default


# --------------------------------------------------------------------------
# config lookup
# --------------------------------------------------------------------------


def host_names(cfg: dict) -> list[str]:
    return sorted(cfg.get("hosts", {}))


def resolve_host(cfg: dict, token: str) -> str | None:
    """Host key for a token that may be the key itself or a host alias."""
    hosts = cfg.get("hosts", {})
    if token in hosts:
        return token
    for name, hcfg in hosts.items():
        if hcfg.get("alias") == token:
            return name
    return None


def resolve_session(cfg: dict, host: str, token: str) -> str | None:
    sessions = cfg["hosts"].get(host, {}).get("sessions", {})
    if token in sessions:
        return token
    for name, scfg in sessions.items():
        if scfg.get("alias") == token:
            return name
    return None


def word_owners(cfg: dict, word: str) -> list[tuple[str, str]]:
    """Every entry a word typed on its own names, as (host, session): a
    host's key or alias gives (host, ""), a session's alias (host, session).

    More than one is a clash, not a choice: the editor refuses a word that
    is taken, and the lookup reports one a hand edit made ambiguous, rather
    than connect to whichever entry the file happens to list first.
    """
    owners = []
    for host, hcfg in sorted((cfg.get("hosts") or {}).items()):
        if word in (host, hcfg.get("alias")):
            owners.append((host, ""))
        for sess, scfg in sorted((hcfg.get("sessions") or {}).items()):
            if scfg.get("alias") == word:
                owners.append((host, sess))
    return owners


def describe_owner(owner: tuple[str, str]) -> str:
    host, session = owner
    return "%s+%s" % (host, session) if session else "host %s" % host


def word_clash(cfg: dict, word: str, own: tuple[str, str]) -> str:
    """What else word names, for a message; "" when it is free for own, the
    (host, session) being edited, which may keep a word it already has."""
    return ", ".join(describe_owner(owner) for owner in word_owners(cfg, word)
                     if owner != own)


def ssh_target(cfg: dict, host: str) -> str:
    return cfg["hosts"].get(host, {}).get("target") or host


class Overrides(NamedTuple):
    """Per-call answers to the boolean fields; None means "use the config".

    Bundled rather than passed one parameter each, because they travel together
    through connect(), every route_* and the subcommand shim, and a parameter
    apiece would make six-argument signatures of all of them.
    """
    ecf: bool | None = None
    waypipe: bool | None = None


NO_OVERRIDES = Overrides()


def resolve_flag(cfg: dict, host: str, session: str, field: str,
                 override: bool | None) -> bool:
    """Whether a boolean field is on: the flag, then session, then host."""
    if override is not None:
        return override
    hcfg = cfg["hosts"].get(host, {})
    scfg = (hcfg.get("sessions") or {}).get(session, {})
    if field in scfg:
        return bool(scfg[field])
    return bool(hcfg.get(field))


def describe_host(cfg: dict, host: str) -> str:
    """`devbox  (alias c, -> target, ecf, default)` -- one host, with its marks."""
    hcfg = cfg["hosts"].get(host, {})
    marks = []
    if hcfg.get("alias"):
        marks.append("alias %s" % hcfg["alias"])
    if hcfg.get("target") and hcfg["target"] != host:
        marks.append("-> %s" % hcfg["target"])
    marks += [field for field in BOOL_FIELDS if hcfg.get(field)]
    if host == cfg.get("default_host"):
        marks.append("default")
    return "%s%s" % (host, "  (%s)" % ", ".join(marks) if marks else "")


def ssh_known_hosts(path: str = KNOWN_HOSTS_PATH) -> list[str]:
    """Host names from ~/.ssh/known_hosts, deduplicated and sorted.

    A host appears once per key type, hence the dedupe. Hashed entries
    (HashKnownHosts, `|1|...') cannot be reversed, so they are skipped, as are
    wildcard and negated patterns -- none of those is a name you could type.
    One line may list several names comma-separated, and a non-default port
    appears as [host]:port, unwrapped here to the bare host.

    Marker lines are skipped whole: @cert-authority names a CA and a pattern
    it signs for rather than a machine, and @revoked marks a key as untrusted,
    which is the last thing to offer as a suggestion.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []

    names: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if not fields or fields[0].startswith("@") or fields[0].startswith("|"):
            continue
        for pattern in fields[0].split(","):
            if not pattern or pattern.startswith("!"):
                continue
            if "*" in pattern or "?" in pattern:
                continue
            if pattern.startswith("[") and "]" in pattern:
                pattern = pattern[1:pattern.index("]")]
            if pattern:
                names.add(pattern)
    return sorted(names)


def dialed_names(cfg: dict) -> dict[str, list[str]]:
    """Every name the registered hosts answer to -- entry name, alias, and
    the host part of the target, so me@mac.local counts for mac.local --
    with the entries that use it."""
    dialed: dict[str, list[str]] = {}
    for host, hcfg in sorted((cfg.get("hosts") or {}).items()):
        names = {host, hcfg.get("alias") or host,
                 (hcfg.get("target") or host).rpartition("@")[2]}
        for name in names:
            dialed.setdefault(name, []).append(host)
    return dialed


def ssh_host_candidates(cfg: dict) -> list[tuple[str, str]]:
    """known_hosts entries as (host, note), the ones no entry dials yet first.

    A machine some entry already reaches is kept, after the fresh ones and
    noted with the entry's name, rather than hidden: it is a different
    destination for every user, so registering it again as root@ is a real
    thing to want.
    """
    dialed = dialed_names(cfg)
    known = ssh_known_hosts()
    fresh = [(host, "") for host in known if host not in dialed]
    taken = [(host, "registered as %s" % ", ".join(sorted(set(dialed[host]))))
             for host in known if host in dialed]
    return fresh + taken


def host_hint(cfg: dict, ssh_hosts: bool = False) -> str:
    """What is registered, for the messages that reject a host name.

    A rejected host is nearly always a typo or a forgotten alias, and the
    answer is the same list every time -- so print it rather than send the
    reader off to `sshtsf -l`. Indented to sit under the `sshtsf: ' prefix
    of the line it follows.

    ssh_hosts adds the unregistered machines, from the ssh-lists file and
    then known_hosts. Right where the answer might be "add one of those",
    wrong for -l/-L, whose subject can only ever be a host registered here.
    """
    hosts = host_names(cfg)
    if hosts:
        out = "        registered:\n" + "\n".join(
            "          %s" % describe_host(cfg, host) for host in hosts)
    else:
        out = "        nothing registered yet; run `sshtsf` to add a host"

    if ssh_hosts:
        # Only the fresh ones: the registered ones are in the list above.
        fresh = [(host, where) for host, note, where in host_candidates(cfg) if not note]
        for where in ("ssh-lists", "~/.ssh/known_hosts"):
            candidates = [host for host, src in fresh if src == where]
            if not candidates:
                continue
            shown = candidates[:MAX_KNOWN_HOSTS]
            out += "\n        in %s:\n" % where + textwrap.fill(
                ", ".join(shown), width=76,
                initial_indent="          ", subsequent_indent="          ")
            if len(candidates) > len(shown):
                out += "\n          ... and %d more" % (
                    len(candidates) - len(shown))
    return out


def describe_session(cfg: dict, host: str, name: str, scfg: dict) -> str:
    bits = [name]
    if scfg.get("alias"):
        bits.append("(%s)" % scfg["alias"])
    if scfg.get("folder"):
        bits.append("~/%s" % scfg["folder"])
    if scfg.get("command"):
        bits.append("-> %s" % scfg["command"])
    bits += ["+%s" % field for field in BOOL_FIELDS
             if resolve_flag(cfg, host, name, field, None)]
    return "  ".join(bits)


# --------------------------------------------------------------------------
# remote interaction
# --------------------------------------------------------------------------


# Depth of the folder listing. 4 reaches src/<org>/<repo>/<pkg>, which
# is what a folder usually points at; fuzzy matching then narrows it.
FOLDER_DEPTH = 4

# Directory names pruned from the listing. These are the ones that explode the
# count -- a workspace's build output and dependency trees -- not paths anyone
# selects as a working directory. Dotdirs are pruned separately.
PRUNE_DIRS = ("build", "node_modules", "target", "env", "cdk.out",
              "__pycache__", "dist", ".git")


def remote_dirs(target: str, depth: int = FOLDER_DEPTH) -> list[str]:
    """Directories under the remote $HOME, relative, for folder selection.

    A flat recursive list so fuzzy matching can jump straight to a package:
    typing "we/src/api" narrows to src/webapp/src/api.
    Heavy trees (build output, node_modules, ...) are pruned, which is what
    keeps a workspace listing in the hundreds rather than thousands.
    """
    prunes = " -o ".join("-name %s" % shlex.quote(d) for d in PRUNE_DIRS)
    # The prune arm matches dotdirs and PRUNE_DIRS at any level; the print arm
    # emits every other directory. sed strips the leading "./".
    script = (
        "cd ~ && find . -mindepth 1 -maxdepth %d "
        "\\( -name '.*' -o %s \\) -prune "
        "-o -type d -print 2>/dev/null | sed 's|^\\./||' | sort"
        % (depth, prunes)
    )
    try:
        proc = subprocess.run(["ssh", target, script], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=60)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def show_folder(folder: str) -> None:
    print("  folder: %s" % ("~/" + folder if folder else "~ (none)"),
          file=sys.stderr)


def prompt_folder(target: str, current: str = "", label: str = "") -> str:
    """Ask for a folder: type one, `?' to fuzzy-pick from a listing, blank for none.

    Returns the folder relative to ~, "" for none. Updating an existing
    folder, blank keeps it and `-' clears it, so the current one is the
    prompt's default and pre-fills fzf's query. Leaving the picker keeps it
    as well -- none, for a new session -- rather than end the whole walk, and
    so does a blank at the prompt that stands in for a listing that failed.
    """
    where = " for %s" % label if label else ""
    if current:
        typed = ask("  folder relative to ~%s (? to browse, - for none)" % where,
                    current)
    else:
        typed = ask("  folder relative to ~%s (blank for none, ? to browse)" % where)
    if typed == "-":
        return ""
    if typed != "?":
        return typed.strip().rstrip("/")

    print("  listing %s ..." % target, file=sys.stderr)
    dirs = remote_dirs(target)
    if not dirs:
        print("  (could not list %s; type a path if you want one)" % target,
              file=sys.stderr)
        if current:
            typed = ask("  folder relative to ~ (- for none)", current)
        else:
            typed = ask("  folder relative to ~ (blank for none)")
        return "" if typed == "-" else typed.strip().rstrip("/")

    chosen = pick(dirs, "folder>",
                  "remote folder (abort keeps %s)" % (current or "none"),
                  free_text=True, query=current)
    if chosen is None:
        return current
    return chosen.strip().rstrip("/")


def remote_sh(script: str) -> list[str]:
    """`sh -c SCRIPT' as ssh command words, with REMOTE_PATH ahead of the PATH.

    Every command sshtsf runs on a remote goes through this, so the probes
    resolve the same tmux the connection will. sh rather than the login shell
    for the script itself: the script is POSIX sh and the login shell need
    not be (fish has no `VAR=value; cmd'). Unquoted, for the caller to quote
    once along with its other words.
    """
    return ["sh", "-c", 'PATH="%s:$PATH"; %s' % (REMOTE_PATH, script)]


def remote_tmux(target: str) -> str:
    """Which tmux a non-interactive `ssh target tmux' resolves, as "PATH (VERSION)".

    Empty when the question cannot be answered. Costs a second connection, so
    it is asked only once list-sessions has already failed without saying why.
    """
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", target]
        + [shlex.quote(word) for word in remote_sh("command -v tmux && tmux -V")],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc.returncode != 0:
        return ""
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return ""
    return "%s (%s)" % (lines[0], lines[1])


def live_sessions(target: str) -> tuple[list[str], str]:
    """Live remote sessions, and -- the point of the tuple -- why there are none.

    tmux can exit nonzero in silence: a client too old to speak to the running
    server does, which is what a host whose non-interactive PATH finds a
    different tmux from its panes' has. Collapsing that to an empty list hid a
    PATH problem behind the same line as a host with no server, so the reason is
    carried out instead: tmux's own words when it said any, and otherwise the
    binary an ssh command actually resolved, since that mismatch IS the failure.

    Tab-separated, and `-u' for the same reason connect passes it: the ssh
    command's shell sets no locale, so tmux takes the client for non-UTF-8
    and sanitizes what it prints for one, the tabs to underscores included.
    """
    fmt = "#{session_name}\t#{session_windows}\t#{?session_attached,attached,detached}"
    proc = subprocess.run(
        ["ssh", target] + [shlex.quote(word) for word in
                           remote_sh("tmux -u list-sessions -F %s" % shlex.quote(fmt))],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode == 0:
        return [line for line in proc.stdout.splitlines() if line.strip()], ""

    err = " ".join(proc.stderr.split())
    if err:
        return [], err
    which = remote_tmux(target)
    if which:
        return [], ("tmux exited %d without a message; `ssh %s tmux' is %s"
                    % (proc.returncode, target, which))
    return [], ("tmux exited %d without a message, and no tmux is on the "
                "non-interactive PATH" % proc.returncode)


def ecf_local_socket() -> str | None:
    """The local Emacs server socket, or None if no server is running.

    Asks emacsclient for server-socket-dir/server-name, then confirms the path
    is really a socket -- an ungraceful Emacs exit leaves a
    plain file behind, and forwarding that would fail at bind time.
    """
    try:
        proc = subprocess.run(
            ["emacsclient", "-e", "(expand-file-name server-name server-socket-dir)"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    path = proc.stdout.strip().strip('"')
    if not path:
        return None
    try:
        if not stat.S_ISSOCK(os.stat(path).st_mode):
            return None
    except OSError:
        return None
    return path


def waypipe_local_display() -> tuple[str | None, str]:
    """The local compositor socket, or None and the reason there isn't one.

    The parallel of ecf_local_socket, down to confirming the path is really a
    socket. Two differences: waypipe has to exist here as well as on the remote,
    and there is no single fix for "unavailable" -- an uninstalled waypipe, a
    machine with no compositor, and an unset XDG_RUNTIME_DIR (macOS, always)
    call for quite different things -- so the reason is returned to be printed.
    """
    if shutil.which("waypipe") is None:
        return None, "waypipe is not on PATH"

    display = os.environ.get("WAYLAND_DISPLAY") or ""
    if not display:
        return None, "WAYLAND_DISPLAY is unset; waypipe needs a Wayland session"

    # libwayland reads an absolute WAYLAND_DISPLAY as the socket path itself,
    # and anything else as a name under XDG_RUNTIME_DIR.
    if os.path.isabs(display):
        path = display
    else:
        runtime = os.environ.get("XDG_RUNTIME_DIR") or ""
        if not runtime:
            return None, ("XDG_RUNTIME_DIR is unset, so WAYLAND_DISPLAY=%s "
                          "cannot be resolved" % display)
        path = os.path.join(runtime, display)

    try:
        if not stat.S_ISSOCK(os.stat(path).st_mode):
            return None, "%s is not a socket" % path
    except OSError:
        return None, "no compositor socket at %s" % path
    return path, ""


def waypipe_remote_bin() -> str:
    """The waypipe the remote will be asked to run: --remote-bin's value, or "".

    waypipe reads its options with getopt_long, so both `--remote-bin PATH'
    and `--remote-bin=PATH' spell it.
    """
    for i, word in enumerate(WAYPIPE_OPTS):
        if word == "--remote-bin" and i + 1 < len(WAYPIPE_OPTS):
            return WAYPIPE_OPTS[i + 1]
        if word.startswith("--remote-bin="):
            return word[len("--remote-bin="):]
    return ""


def tab_title(title: str) -> str:
    """The escapes that name the terminal tab: Konsole's OSC 30, then the
    generic window title (OSC 0) for terminals whose tab shows that."""
    return "\033]30;%s\007\033]0;%s\007" % (title, title)


def waypipe_remote_missing(target: str) -> str:
    """Why `waypipe ssh' would die on the far side, or "" when it should not.

    waypipe_local_display, one hop further out: waypipe has to exist at both
    ends, and a remote without it fails after the handshake with waypipe's own
    words, past the point where anything can be done about it. Asking first
    costs a BatchMode round trip, paid only when waypipe is on, and buys the
    choice between a plain connection and none.

    Honours --remote-bin in SSHTSF_WAYPIPE_OPTS, the knob for exactly the host
    whose non-interactive PATH lacks waypipe. A probe that cannot answer -- a
    host that only takes a password, a timeout -- is taken as a yes, leaving
    the real connection to be the judge, as valid_remote_name does with git.
    """
    remote_bin = waypipe_remote_bin()
    if remote_bin:
        check = "test -x %s" % shlex.quote(remote_bin)
        what = "%s is not executable on %s" % (remote_bin, target)
    else:
        check = "command -v waypipe"
        what = "no waypipe on the non-interactive PATH of %s" % target
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", target, check],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    # 255 is ssh itself failing to connect; anything else is the remote
    # shell's verdict.
    if proc.returncode in (0, 255):
        return ""
    return what


def remote_user_guess(target: str) -> str:
    """The login a destination most likely gives: its user@, else ours.

    ssh's own default when neither the destination nor ssh_config names one;
    a User line in ssh_config is invisible from here, which is why this is a
    guess and a real connection asks the remote instead.
    """
    if "@" in target:
        return target.rpartition("@")[0]
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return str(os.getuid())


def connect(cfg: dict, host: str, session: str, dry_run: bool = False,
            over: Overrides = NO_OVERRIDES) -> int:
    scfg = cfg["hosts"][host].get("sessions", {}).get(session, {})
    target = ssh_target(cfg, host)
    ecf = resolve_flag(cfg, host, session, "ecf", over.ecf)
    waypipe = resolve_flag(cfg, host, session, "waypipe", over.waypipe)

    # A forward that cannot be set up is a warning, never a refusal, whether
    # it was asked for by flag or by config: the session is the point, and a
    # forward is a convenience on top of it. Failing here would only send you
    # back to retype the command without the flag. The reason is printed
    # because it is the whole difference between "install waypipe" and "you
    # are on a Mac".
    if waypipe:
        display, why = waypipe_local_display()
        if display is None:
            print("sshtsf: no local Wayland display (%s); connecting without it"
                  % why, file=sys.stderr)
            waypipe = False
    if waypipe:
        why = waypipe_remote_missing(target)
        if why:
            print("sshtsf: %s; connecting without Wayland" % why, file=sys.stderr)
            waypipe = False

    ecf_local = None
    if ecf:
        ecf_local = ecf_local_socket()
        if ecf_local is None:
            print("sshtsf: no local Emacs server socket (start Emacs first); "
                  "connecting without the forward", file=sys.stderr)

    # The remote socket path is settled on a connection of its own, ahead of
    # the real one, which does two things at once. It names the socket after
    # the login the remote actually gives -- `id -un' there, not the user@
    # dialed or ssh_config's User, which is the only way the path is sure to
    # match what that login's emacsclient-auto looks for. And it clears a
    # leftover: sshd does not honour StreamLocalBindUnlink, so after an
    # ungraceful disconnect the file makes the -R bind fail, and the bind
    # happens at session setup, before any remote command could remove it.
    # One socket per login, so a second ecf connection replaces the first
    # one's forward; harmless, since both point at the same Emacs. A file
    # that cannot be removed would fail the bind just the same, so the
    # forward is dropped with the reason rather than attempted. A dry run
    # dials nothing, and guesses the login the way ssh would by default.
    cleanup: list[str] = []
    ecf_remote = ""
    if ecf_local:
        cleanup = ["ssh", target] + [shlex.quote(word) for word in remote_sh(
            'p=%s-$(id -un); rm -f "$p" && echo "$p"' % shlex.quote(ECF_REMOTE_SOCKET))]
        if dry_run:
            ecf_remote = "%s-%s" % (ECF_REMOTE_SOCKET, remote_user_guess(target))
        else:
            proc = subprocess.run(cleanup, text=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
            lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
            ecf_remote = lines[-1] if lines else ""
            if proc.returncode != 0 or not ecf_remote.startswith(ECF_REMOTE_SOCKET):
                why = " ".join(proc.stderr.split()) or "exit %d" % proc.returncode
                print("sshtsf: could not clear the Emacs socket on %s (%s); "
                      "connecting without the forward" % (target, why),
                      file=sys.stderr)
                ecf_local = None
                cleanup = []
    ecf_port = None
    if ecf_local:
        ecf_port = scfg.get("ecf_port") or cfg["hosts"][host].get("ecf_port")

    # What the session has to be told about this connection, as (name, shell
    # word) pairs; the word is spliced into an `sh -c' script as-is, so a
    # literal is quoted here and a `"$VAR"' is left for the remote sh to expand.
    session_env: list[tuple[str, str]] = []
    if waypipe:
        # Teach the session waypipe's display name, rather than trusting the
        # remote's tmux to carry it: WAYLAND_DISPLAY only joined the default
        # update-environment in tmux 3.7, and on anything older the session
        # never learns it at all -- `tmux show-environment WAYLAND_DISPLAY'
        # answers "unknown variable" and GUI applications quietly open on the
        # remote's own screen instead of here.
        #
        # It has to be a shell that WAYPIPE started. waypipe execs the command
        # itself (`waypipe ... server tmux ...'), and only its children have the
        # forwarded WAYLAND_DISPLAY -- the ssh login shell that parses this line
        # runs earlier and would expand it to the remote's own display, or to
        # nothing. So `sh -c' below, with $WAYLAND_DISPLAY left for it to expand.
        session_env.append(("WAYLAND_DISPLAY", '"$WAYLAND_DISPLAY"'))
    if ecf_local:
        # Tell the session the name this end dialed, so the remote's
        # emacs-remote can build a TRAMP prefix the local Emacs can actually
        # connect back through. The remote cannot work that out itself: its
        # `hostname' knows nothing of an mDNS `.local' suffix or an ssh_config
        # alias, and a name that resolves from the remote need not resolve from
        # here. The destination as dialed, user@ and all, since TRAMP resolves
        # it through the same ssh_config; emacs-remote prepends the login user
        # when there is none.
        session_env.append(("EMACS_REMOTE_TARGET", shlex.quote(target)))

    folder = scfg.get("folder") or ""

    def shell_instead(reason: str) -> str:
        """A `{ ...; }' that gives up on tmux: warn, then the login shell.

        The worst case is a plain ssh, not a dead connection. The shell starts
        in the session's folder with what the session would have been told
        exported into it -- except a `"$VAR"' entry, a value the shell already
        holds. A relay started above is stopped, so the exec'd shell leaves
        nothing behind.
        """
        return ('{ echo "sshtsf: %s; a plain shell instead" >&2; ' % reason
                + ('[ -z "$SOCAT_PID" ] || kill $SOCAT_PID 2>/dev/null; '
                   if ecf_port else "")
                + ("cd %s 2>/dev/null; " % shlex.quote(folder) if folder else "")
                + "".join("export %s=%s; " % (name, word)
                          for name, word in session_env if not word.startswith('"$'))
                + 'exec "${SHELL:-sh}" -l; }')

    # -u because tmux is the ssh command, so the remote shell is
    # non-interactive, never reads .zshrc and never sets LC_ALL -- and the
    # client then renders every non-ASCII cell as an underscore. -u asserts
    # UTF-8 without depending on the remote's locales.
    #
    # Create, set, then attach, in three steps rather than one `-A': -A would
    # attach before the variables could be set, `-e' applies only when tmux
    # creates the session, never when it attaches to a live one -- and a live
    # session is exactly the one holding values from a connection that is
    # gone -- and a create that fails can only fall back when it is a step of
    # its own.
    #
    # has-session rather than `new-session -Ad', because -A makes new-session
    # behave as attach-session and -d stops meaning detached there -- so -Ad
    # on an EXISTING session attaches anyway, and the rest of the chain never
    # runs. (-AD would work, but it also detaches whoever else is attached,
    # which is not ours to do.)
    #
    # `=name' is an exact-match target: without it a session name that
    # prefixes another could resolve to the wrong one.
    tgt = shlex.quote("=" + session)
    create_words = ["tmux", "-u", "new-session", "-d"]
    if folder:
        create_words += ["-c", folder]
    create_words += ["-s", session]
    create = " ".join(shlex.quote(word) for word in create_words)
    if scfg.get("command"):
        # A configured command is a command line, not a bare argv[0]: split it
        # the way a shell would so `emacs -nw .` works as written. One that
        # will not start (not installed there, a typo) takes the server down
        # with it on a fresh host; the session is then made without it.
        with_command = create + " " + " ".join(
            shlex.quote(word) for word in shlex.split(scfg["command"]))
        create = ('%s || { echo "sshtsf: the session command failed to start; '
                  'a session without it instead" >&2; %s; }' % (with_command, create))

    relay = ""
    if ecf_port:
        # socat is checked for on the remote rather than assumed: without the
        # check a missing one dies in the background with a bare `not found'
        # and the forwarded port sits there unrelayed. With it, the session is
        # warned once and attached anyway -- the same generosity as the local
        # checks above, at no extra round trip. After the tmux check, so a
        # host without tmux never starts a relay it would leave behind.
        relay = (
            "if command -v socat >/dev/null; then "
            "socat UNIX-LISTEN:%s,fork TCP:127.0.0.1:%s & "
            "SOCAT_PID=$!; "
            "else echo \"sshtsf: no socat on the remote; the Emacs relay is off\" >&2; fi; "
            % (shlex.quote(ecf_remote), ecf_port))
    # The attach is not exec'd either: a session whose command died between
    # the create and the attach (tmux can report the create done first), or
    # a server that goes away mid-session, ends the attach nonzero, and that
    # is the last chance for a shell rather than a closed connection. A
    # detach and a killed session both exit 0, so neither trips it.
    attach = "tmux -u attach-session -t %s || %s" % (
        tgt, shell_instead("could not attach to the session"))
    script = (
        "command -v tmux >/dev/null || %s; " % shell_instead("no tmux on the remote")
        + relay
        + "tmux has-session -t %s 2>/dev/null || %s || %s; "
        % (tgt, create, shell_instead("could not create the session"))
        + "".join("tmux set-environment -t %s %s %s; " % (tgt, name, word)
                  for name, word in session_env)
        + attach
        + ("; [ -z \"$SOCAT_PID\" ] || kill $SOCAT_PID 2>/dev/null" if ecf_port else ""))
    remote = remote_sh(script)

    # No ExitOnForwardFailure: a bind that fails despite the cleanup is ssh's
    # own warning and a session without the forward, not a dead connection.
    sshopts: list[str] = []
    if ecf_local:
        sshopts = ["-R", "%s:%s" % (ecf_port or ecf_remote, ecf_local)]

    # -t before the target, not after it. ssh itself accepts either, but
    # waypipe takes the first non-option word as the destination and everything
    # past it as the command to run -- so a trailing -t becomes argv[0] of that
    # command and it dies with `Failed to run program "-t"'.
    #
    # Quote for the remote shell: ssh joins its command words with spaces and
    # the far side re-parses them, so an unquoted path with a space would
    # arrive as two arguments.
    argv = ["ssh"] + sshopts + ["-t", target] + \
        [shlex.quote(word) for word in remote]

    if waypipe:
        # waypipe takes its options before the mode word and passes everything
        # after `ssh' through, so the whole command above rides along as ssh
        # arguments -- which is what lets waypipe and ecf compose, as two
        # independent -R forwards on one connection. waypipe adds a -t of its
        # own; a doubled -t only means -tt, which is harmless here since there
        # is always a terminal. No --display: waypipe's randomized
        # per-connection name is fine, because the session is told the name
        # above, and a prompt hook that re-reads `tmux show-environment' can
        # carry it into the panes that predate this connection.
        argv = ["waypipe"] + WAYPIPE_OPTS + ["ssh"] + argv[1:]

    if dry_run:
        if cleanup:
            print(" ".join(cleanup))
        print(" ".join(argv))
        return 0

    remember(cfg, host, session)
    print("sshtsf: %s -> %s%s%s"
          % (target, session, " +ecf" if sshopts else "",
             " +waypipe" if waypipe else ""), file=sys.stderr)
    if waypipe and sys.stdout.isatty():
        # Konsole names a tab after its foreground process and only knows an
        # ssh session when that process is ssh itself. Behind waypipe it sees
        # waypipe, so name the tab here. Nothing runs after the exec, so the
        # name stays on the tab once the session ends; a plain session is
        # left to the terminal, which resets its own title on logout.
        sys.stdout.write(tab_title("%s:%s" % (host, session)))
        sys.stdout.flush()
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:
        sys.exit("sshtsf: cannot exec %s: %s" % (argv[0], exc))


def remember(cfg: dict, host: str, session: str) -> None:
    cfg["last"] = {"host": host, "session": session}
    try:
        save_config(cfg)
    except OSError as exc:
        # Not worth aborting a connection over.
        print("sshtsf: could not save last-used (%s)" % exc, file=sys.stderr)


# --------------------------------------------------------------------------
# the editor: one prompt walk registers a host or session, and edits it
# --------------------------------------------------------------------------

HOST_SETTINGS = "* host settings"

# Typed at a field to clear it; at a name prompt, to remove the entry.
CLEAR = "-"


def ask_text(prompt: str, current: str = "", editing: bool = False,
             none: str = "none") -> str:
    """A string field. New: blank for none. Editing: blank keeps the current
    value and `-' clears it; the hint says which. Returns "" for none."""
    if editing and current:
        reply = ask("%s (- for %s)" % (prompt, none), current)
    else:
        reply = ask("%s (blank for %s)" % (prompt, none))
    return "" if reply.strip() == CLEAR else reply.strip()


def yes_no(reply: str) -> bool | None:
    """True or False for a yes/no word; None for one that is neither."""
    word = reply.strip().lower()
    if word in TRUE_WORDS + ("y",):
        return True
    if word in FALSE_WORDS + ("n",):
        return False
    return None


def ask_bool(prompt: str, current: bool | None = None, editing: bool = False,
             absent: str = "none") -> bool | None:
    """A boolean field; None means "not set". New, it is a (y/N) where only a
    yes sets it, since an absent field already means no. Editing, blank
    keeps the current value and `-' unsets it, which for a session means
    following the host; absent names what unset means there, for the hint.
    A word that is neither yes nor no is asked again rather than taken as
    no, so a typo cannot quietly turn a forward off."""
    if not editing:
        return True if said_yes(ask("%s (y/N)" % prompt)) else None
    while True:
        if current is None:
            reply = ask("%s (y/n, blank for %s)" % (prompt, absent))
            if not reply or reply == CLEAR:
                return None
        else:
            reply = ask("%s (y/n, - for %s)" % (prompt, absent),
                        "yes" if current else "no")
            if reply == CLEAR:
                return None
        answer = yes_no(reply)
        if answer is not None:
            return answer
        print("  y or n: %s" % reply, file=sys.stderr)


def ask_port(prompt: str, current: int | None, editing: bool) -> int | None:
    while True:
        reply = ask_text(prompt, str(current) if current else "", editing)
        if not reply:
            return None
        if reply.isdigit() and int(reply) > 0:
            return int(reply)
        print("  not a port: %s" % reply, file=sys.stderr)


def ask_word(cfg: dict, own: tuple[str, str], asker) -> str:
    """asker's answer, asked again while it names another entry (see
    word_owners). Blank and `-' pass: neither is a word anyone types."""
    while True:
        word = asker()
        clash = word_clash(cfg, word, own) if word and word != CLEAR else ""
        if not clash:
            return word
        print("  %s already names %s" % (word, clash), file=sys.stderr)


def set_field(holder: dict, field: str, value) -> None:
    """Store a value, or drop the key when it is None or "": an absent
    string field is the same as an empty one, and an absent boolean is
    what lets a session follow the host."""
    if value is None or value == "":
        holder.pop(field, None)
    else:
        holder[field] = value


def lists_logins() -> list[str]:
    """The logins of the ssh-lists files, in their order and once each; none
    without a file. The other tools' list of machines, which is the first
    place to look for one to register here."""
    try:
        entries = lists.load_all().logins
    except lists.ToolError:
        return []
    return list(dict.fromkeys(entry.login for entry in entries))


def host_candidates(cfg: dict) -> list[tuple[str, str, str]]:
    """(destination, note, source) to offer for a new host: the ssh-lists
    logins, then the known_hosts machines the lists do not name, the fresh
    ones first. A login is `[user@]host', exactly what a target holds, so
    it is offered as is; one an entry already dials is noted with the
    entry's name, as the known_hosts ones are (see ssh_host_candidates).
    source says which file it came from, for a hint that groups by it."""
    dialed = dialed_names(cfg)
    listed = lists_logins()
    out = []
    for login in listed:
        names = {login, login.rpartition("@")[2]}
        hosts = sorted({host for name in names for host in dialed.get(name, [])})
        out.append((login, "registered as %s" % ", ".join(hosts) if hosts else "",
                    "ssh-lists"))
    machines = {login.rpartition("@")[2] for login in listed}
    return out + [(host, note, "~/.ssh/known_hosts")
                  for host, note in ssh_host_candidates(cfg) if host not in machines]


def pick_host_candidate(cfg: dict) -> str:
    """Choose a destination for a new host, or type one; "" when there is
    nothing to offer or the picker was left."""
    labels = {"%s  (%s)" % (host, note) if note else host: host
              for host, note, _ in host_candidates(cfg)}
    if not labels:
        return ""
    choice = pick(list(labels), "host>",
                  "logins in ssh-lists, then ~/.ssh/known_hosts (or type a name)",
                  free_text=True) or ""
    return labels.get(choice, choice)


def edit_host(cfg: dict, name: str = "", existing: dict | None = None) -> str | None:
    """The prompt walk for a host: registers a new one (existing None), or
    edits, renames or removes the one whose entry is given. Returns the
    host's key afterwards, None once it is removed.

    The one walk serves both, so a field is asked the same way whether it
    is being set for the first time or changed; only the hints differ.
    """
    hosts = cfg["hosts"]
    editing = existing is not None
    hcfg = dict(existing) if editing else {}

    if editing:
        new_name = ask_word(cfg, (name, ""), lambda: ask(
            "  name (what you will type; - removes the host)", name))
        if new_name == CLEAR:
            return None if remove_host(cfg, name) else name
        target = ask("  ssh destination ([user@]host; - for the name itself)",
                     hcfg.get("target") or name)
        if target == CLEAR:
            target = ""
    else:
        # Nothing typed yet: offer what the other tools reach and what ssh
        # has reached, so registering a host is a selection rather than a
        # retype. free_text keeps a name that is in neither, and aborting the
        # picker just falls through to the prompt below.
        if not name:
            name = pick_host_candidate(cfg)
        # What was picked or typed is a destination; the name may well be
        # shortened from it, so it is the destination's default, and the
        # name's default is its machine part, sans user.
        target = name
        while True:
            name = ask("  name for this [user@]host (what you will type)",
                       target.rpartition("@")[2])
            if not name:
                sys.exit("sshtsf: a name for the [user@]host is required")
            if name in hosts:
                print("sshtsf: %s already registered" % name, file=sys.stderr)
                return name
            # Not a host, but maybe another's alias or a session's.
            clash = word_clash(cfg, name, (name, ""))
            if not clash:
                break
            print("  %s already names %s" % (name, clash), file=sys.stderr)
        new_name = name
        # The destination is what ssh dials, user and all; there is no user
        # field of its own. Asked for separately only when the destination
        # names none, and a blank leaves it to ssh (~/.ssh/config, or your
        # own name).
        target = ask("  ssh destination ([user@]host)", target or name)
        if "@" not in target:
            user = ask("  user on it (blank for ssh's default)")
            if user:
                target = "%s@%s" % (user, target)
    set_field(hcfg, "target", target if target and target != new_name else "")

    set_field(hcfg, "alias", ask_word(cfg, (name, ""), lambda: ask_text(
        "  alias", hcfg.get("alias", ""), editing)))
    # The host-wide defaults: a yes here is inherited by every session on the
    # host, and edit_session then skips the same question for a new session
    # rather than ask it again.
    ecf = ask_bool("  forward the local Emacs socket, for every session?",
                   hcfg.get("ecf"), editing)
    set_field(hcfg, "ecf", ecf)
    if ecf or hcfg.get("ecf_port"):
        set_field(hcfg, "ecf_port", ask_port(
            "  relay it through a TCP port, for an SELinux host",
            hcfg.get("ecf_port"), editing))
    set_field(hcfg, "waypipe", ask_bool("  forward Wayland, for every session?",
                                        hcfg.get("waypipe"), editing))

    if editing and new_name != name:
        hosts.pop(name)
        if cfg.get("default_host") == name:
            cfg["default_host"] = new_name
        if (cfg.get("last") or {}).get("host") == name:
            cfg["last"]["host"] = new_name
        name = new_name
    hcfg.setdefault("sessions", {})
    hosts[name] = hcfg

    # Which host the picker offers first. A first host is it without asking;
    # after that it is a choice, so it is asked when editing, where the
    # answer is in front of you, rather than at every registration.
    if editing:
        first = yes_no(ask("  offer this host first? (y/n)",
                           "yes" if cfg.get("default_host") == name else "no"))
        if first:
            cfg["default_host"] = name
        elif cfg.get("default_host") == name:
            cfg.pop("default_host")
    elif not cfg.get("default_host"):
        cfg["default_host"] = name

    save_config(cfg)
    print("sshtsf: saved host %s" % name, file=sys.stderr)
    return name


def remove_host(cfg: dict, host: str) -> bool:
    """Drop a host after a confirmation, sessions and all. False if kept."""
    sessions = cfg["hosts"][host].get("sessions") or {}
    what = ("%s and its %d session(s)" % (host, len(sessions)) if sessions
            else "host %s" % host)
    if not said_yes(ask("  remove %s? (y/N)" % what)):
        print("sshtsf: kept %s" % host, file=sys.stderr)
        return False
    del cfg["hosts"][host]
    if cfg.get("default_host") == host:
        cfg.pop("default_host", None)
    if (cfg.get("last") or {}).get("host") == host:
        cfg.pop("last", None)
    save_config(cfg)
    print("sshtsf: removed host %s" % host, file=sys.stderr)
    return True


def remove_session(cfg: dict, host: str, session: str) -> bool:
    if not said_yes(ask("  remove session %s from %s? (y/N)" % (session, host))):
        print("sshtsf: kept %s" % session, file=sys.stderr)
        return False
    del cfg["hosts"][host]["sessions"][session]
    last = cfg.get("last") or {}
    if last.get("host") == host and last.get("session") == session:
        cfg.pop("last", None)  # else `last` points at a session that is gone
    save_config(cfg)
    print("sshtsf: removed %s from %s" % (session, host), file=sys.stderr)
    return True


def edit_session(cfg: dict, host: str, name: str = "",
                 existing: dict | None = None) -> str | None:
    """The prompt walk for a session on host: edit_host's counterpart.
    Returns the session's key, None once it is removed; a new name that is
    already a session is returned as is, nothing asked."""
    hcfg = cfg["hosts"][host]
    sessions = hcfg.setdefault("sessions", {})
    editing = existing is not None
    scfg = dict(existing) if editing else {}

    if editing:
        new_name = ask("  session name (- removes it)", name)
        if new_name == CLEAR:
            return None if remove_session(cfg, host, name) else name
        if new_name != name and new_name in sessions:
            sys.exit("sshtsf: %s already exists on %s" % (new_name, host))
    else:
        name = ask("  session name", name)
        if not name:
            sys.exit("sshtsf: a session name is required")
        found = resolve_session(cfg, host, name)
        if found:
            print("sshtsf: %s already exists on %s" % (found, host), file=sys.stderr)
            return found
        new_name = name

    folder = prompt_folder(ssh_target(cfg, host), scfg.get("folder", ""), label=new_name)
    show_folder(folder)
    set_field(scfg, "folder", folder)

    set_field(scfg, "command", ask_text("  command to run", scfg.get("command", ""),
                                        editing, none="a shell"))

    for field, question in (("ecf", "forward the local Emacs socket?"),
                            ("waypipe", "forward Wayland, for GUI applications?")):
        # Only worth asking a new session when it would change anything: with
        # the field on for the host, a yes is inherited, and the opt-out is
        # the rarer thing, left to -c.
        if editing or not hcfg.get(field):
            set_field(scfg, field, ask_bool(
                "  " + question, scfg.get(field), editing,
                absent="the host's (%s)" % ("on" if hcfg.get(field) else "off")))

    # Asked last, so the whole entry is visible by the time the shorthand
    # for it is chosen. A new one is offered the session name, unless that
    # already names something else: `main' on a second host would be.
    own = (host, name)
    current = scfg.get("alias", "")
    if not editing and not word_clash(cfg, new_name, own):
        current = new_name
    set_field(scfg, "alias", ask_word(cfg, own, lambda: ask_text(
        "  alias for %s+%s" % (host, new_name), current, True)))

    if editing and new_name != name:
        sessions.pop(name)
        last = cfg.get("last") or {}
        if last.get("host") == host and last.get("session") == name:
            last["session"] = new_name
        name = new_name
    sessions[name] = scfg
    save_config(cfg)
    print("sshtsf: saved %s to %s" % (name, CONFIG_PATH), file=sys.stderr)
    return name


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------


def route_add_session(cfg: dict, host: str, name: str = "",
                      dry_run: bool = False,
                      over: Overrides = NO_OVERRIDES) -> int:
    """Register a session interactively, then connect to it."""
    print("sshtsf: new session on %s" % host, file=sys.stderr)
    session = edit_session(cfg, host, name)
    return connect(cfg, host, session, dry_run, over)


def route_add_host(cfg: dict, name: str = "", dry_run: bool = False,
                   over: Overrides = NO_OVERRIDES) -> int:
    """Register a host interactively, then its first session; a name that
    is already registered goes to that host's sessions instead."""
    print("sshtsf: new host", file=sys.stderr)
    before = set(cfg["hosts"])
    host = edit_host(cfg, name)
    if host in before:
        return route_pick_session(cfg, host, dry_run, over)
    return route_add_session(cfg, host, dry_run=dry_run, over=over)


def route_pick_session(cfg: dict, host: str, dry_run: bool = False,
                       over: Overrides = NO_OVERRIDES) -> int:
    """Show this host's sessions, plus an add-new option."""
    labels = session_labels(cfg, host)
    items = list(labels) + [NEW_SESSION]
    choice = pick(items, "session>", "sessions on %s" % host)
    if choice is None:
        return 130
    if choice == NEW_SESSION:
        return route_add_session(cfg, host, dry_run=dry_run, over=over)
    return connect(cfg, host, labels[choice], dry_run, over)


def session_labels(cfg: dict, host: str) -> dict[str, str]:
    sessions = cfg["hosts"].get(host, {}).get("sessions", {}) or {}
    return {describe_session(cfg, host, name, sessions[name]): name
            for name in sorted(sessions)}


def host_labels(cfg: dict) -> dict[str, str]:
    """The registered hosts as the picker shows them, the default (or
    last-used) first so it is one Enter away. The same line `-l' prints, so
    a target that differs from the name (user@, a .local suffix) shows
    here too, where the choice is made."""
    hosts = host_names(cfg)
    default = cfg.get("default_host") or (cfg.get("last") or {}).get("host")
    ordered = ([default] if default in hosts else []) + \
              [h for h in hosts if h != default]
    return {describe_host(cfg, host): host for host in ordered}


def route_pick_host(cfg: dict, dry_run: bool = False,
                    over: Overrides = NO_OVERRIDES) -> int:
    labels = host_labels(cfg)
    items = list(labels) + [NEW_HOST]
    choice = pick(items, "host>", "hosts")
    if choice is None:
        return 130
    if choice == NEW_HOST:
        return route_add_host(cfg, dry_run=dry_run, over=over)
    return route_pick_session(cfg, labels[choice], dry_run, over)


def route_configure(cfg: dict, words: list[str]) -> int:
    """-c: the editor, entered at the host picker, a host, or a session. A
    name that is not registered is registered, as the connect path does
    with an unknown session. Saves and exits; never connects."""
    if not words:
        labels = host_labels(cfg)
        choice = pick(list(labels) + [NEW_HOST], "host>", "hosts to configure")
        if choice is None:
            return 130
        if choice == NEW_HOST:
            print("sshtsf: new host", file=sys.stderr)
            edit_host(cfg)
            return 0
        host = labels[choice]
    else:
        host = resolve_host(cfg, words[0])
        if not host:
            print("sshtsf: new host %s" % words[0], file=sys.stderr)
            edit_host(cfg, words[0])
            return 0

    hcfg = cfg["hosts"][host]
    if len(words) == 2:
        session = resolve_session(cfg, host, words[1])
        if not session:
            print("sshtsf: new session on %s" % host, file=sys.stderr)
            edit_session(cfg, host, words[1])
        else:
            edit_session(cfg, host, session, hcfg["sessions"][session])
        return 0

    labels = session_labels(cfg, host)
    items = [HOST_SETTINGS] + list(labels) + [NEW_SESSION]
    choice = pick(items, "edit>", "%s: its settings, or a session" % host)
    if choice is None:
        return 130
    if choice == HOST_SETTINGS:
        edit_host(cfg, host, hcfg)
    elif choice == NEW_SESSION:
        print("sshtsf: new session on %s" % host, file=sys.stderr)
        edit_session(cfg, host)
    else:
        edit_session(cfg, host, labels[choice], hcfg["sessions"][labels[choice]])
    return 0


# --------------------------------------------------------------------------
# the other actions: -l, -L, --edit
# --------------------------------------------------------------------------


def hosts_named(cfg: dict, token: str | None) -> list[str]:
    """One host, resolved from a token, or all of them; exits on a token
    that names none."""
    if token:
        host = resolve_host(cfg, token)
        if not host:
            sys.exit("sshtsf: unknown host: %s\n%s" % (token, host_hint(cfg)))
        return [host]
    return host_names(cfg)


def cmd_list(cfg: dict, token: str | None = None) -> int:
    if not cfg.get("hosts"):
        print("no hosts registered; run `sshtsf` to add one")
        return 0
    last = cfg.get("last") or {}
    for host in hosts_named(cfg, token):
        print(describe_host(cfg, host))
        sessions = cfg["hosts"][host].get("sessions") or {}
        if not sessions:
            print("    (no sessions)")
        for name in sorted(sessions):
            here = " *" if (last.get("host") == host
                            and last.get("session") == name) else ""
            print("    %s%s" % (describe_session(cfg, host, name, sessions[name]),
                                here))
    return 0


def cmd_live(cfg: dict, token: str | None = None) -> int:
    if not cfg.get("hosts"):
        sys.exit("sshtsf: no hosts registered")
    for host in hosts_named(cfg, token):
        target = ssh_target(cfg, host)
        rows, why = live_sessions(target)
        print("%s:" % (host if target == host else "%s  (-> %s)" % (host, target)))
        if not rows:
            print("    (%s)" % (why or "no tmux server"))
            continue
        known = cfg["hosts"][host].get("sessions", {}) or {}
        for row in rows:
            name, windows, state = (row.split("\t") + ["", ""])[:3]
            mark = "" if name in known else "   [unregistered]"
            print("    %s  %s window%s, %s%s"
                  % (name, windows, "" if windows == "1" else "s", state, mark))
    return 0


def editor_command() -> list[str]:
    """$VISUAL, else $EDITOR, else vi: the convention git and crontab follow.

    Split as a shell would, so `emacsclient -t' works as written.
    """
    for var in ("VISUAL", "EDITOR"):
        words = shlex.split(os.environ.get(var) or "")
        if words:
            return words
    return ["vi"]


def cmd_edit(dry_run: bool = False) -> int:
    """Open the config in the editor, then say whether it still parses.

    A config that does not exist yet is written first, so the editor gets a
    file in a directory that exists rather than a path it may refuse to save
    to. Afterwards the file is read back: a slip in the TOML is reported now,
    with its line, instead of on the next `sshtsf devbox'.
    """
    argv = editor_command() + [CONFIG_PATH]
    if dry_run:
        # Before the config is written: a dry run touches nothing.
        print(" ".join(shlex.quote(word) for word in argv))
        return 0
    if not os.path.exists(CONFIG_PATH):
        save_config({"hosts": {}})
    try:
        rc = subprocess.run(argv).returncode
    except OSError as exc:
        sys.exit("sshtsf: cannot run %s: %s" % (argv[0], exc))
    if rc != 0:
        print("sshtsf: %s exited %d" % (argv[0], rc), file=sys.stderr)
        return rc
    try:
        read_config()
    except ConfigError as exc:
        print("sshtsf: %s" % exc, file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def unknown_host(cfg: dict, token: str) -> None:
    sys.exit("sshtsf: unknown host: %s (`sshtsf -c %s` registers it)\n%s"
             % (token, token, host_hint(cfg, ssh_hosts=True)))


@click.command(cls=logins.Command, help=__doc__)
@click.option("-n", "--new", is_flag=True,
              help="register a new session on HOST (NAME proposed), then connect")
@click.option("-l", "--list", "list_", is_flag=True,
              help="show the registered hosts and sessions")
@click.option("-L", "--live", is_flag=True,
              help="show the live tmux sessions on the remote(s)")
@click.option("-c", "--configure", is_flag=True,
              help="add, edit or remove a host or session")
@click.option("--edit", is_flag=True, help="open the config in $VISUAL / $EDITOR")
@click.option("-e", "--ecf", is_flag=True, help="forward the local Emacs server socket")
@click.option("-E", "--no-ecf", is_flag=True,
              help="do not forward it, whatever the config says")
@click.option("-w", "--waypipe", is_flag=True,
              help="forward Wayland, so GUI applications draw here")
@click.option("-W", "--no-waypipe", is_flag=True,
              help="do not forward Wayland, whatever the config says")
@click.option("--dry-run", is_flag=True,
              help="print the ssh command instead of running it")
@click.argument("words", nargs=-1, metavar="[HOST [SESSION]]")
@click.pass_context
def cli(ctx: click.Context, new: bool, list_: bool, live: bool, configure: bool,
        edit: bool, ecf: bool, no_ecf: bool, waypipe: bool, no_waypipe: bool,
        dry_run: bool, words: tuple[str, ...]) -> int:
    """The action is an option; the words are a host and a session."""
    if ecf and no_ecf:
        ctx.fail("--ecf and --no-ecf are contradictory")
    if waypipe and no_waypipe:
        ctx.fail("--waypipe and --no-waypipe are contradictory")
    actions = [flag for flag, on in (("-n", new), ("-l", list_), ("-L", live),
                                     ("-c", configure), ("--edit", edit)) if on]
    if len(actions) > 1:
        ctx.fail("one of %s at a time" % ", ".join(actions))
    action = actions[0] if actions else ""
    most = {"-n": 2, "-l": 1, "-L": 1, "-c": 2, "--edit": 0, "": 2}[action]
    if len(words) > most:
        ctx.fail("%s takes at most %d word(s): %s"
                 % (action or "sshtsf", most, " ".join(words)))
    if new and not words:
        ctx.fail("-n needs a host: sshtsf -n HOST [NAME]")
    # None means "no override": fall back to the session's or host's setting.
    over = Overrides(ecf=True if ecf else (False if no_ecf else None),
                     waypipe=True if waypipe else (False if no_waypipe else None))
    positional = list(words)

    try:
        cfg = read_config()
    except ConfigError as exc:
        # The one action that has to work on a config that will not parse,
        # since opening it is how that gets fixed.
        if edit:
            print("sshtsf: %s" % exc, file=sys.stderr)
            return cmd_edit(dry_run=dry_run)
        sys.exit("sshtsf: %s" % exc)

    if edit:
        return cmd_edit(dry_run=dry_run)
    if list_:
        return cmd_list(cfg, positional[0] if positional else None)
    if live:
        return cmd_live(cfg, positional[0] if positional else None)
    if configure:
        return route_configure(cfg, positional)

    if new:
        host = resolve_host(cfg, positional[0])
        if not host:
            unknown_host(cfg, positional[0])
        name = positional[1] if len(positional) > 1 else ""
        return route_add_session(cfg, host, name, dry_run=dry_run, over=over)

    if not positional:
        if not cfg.get("hosts"):
            print("sshtsf: no config yet; let's make one", file=sys.stderr)
            return route_add_host(cfg, dry_run=dry_run, over=over)
        return route_pick_host(cfg, dry_run=dry_run, over=over)

    if len(positional) == 1:
        token = positional[0]
        owners = word_owners(cfg, token)
        if len(owners) > 1:
            sys.exit("sshtsf: %s is ambiguous: it names %s\n"
                     "        `sshtsf -c` gives all but one of them another name or alias"
                     % (token, ", ".join(describe_owner(owner) for owner in owners)))
        if owners:
            host, session = owners[0]
            if session:
                return connect(cfg, host, session, dry_run, over)
            return route_pick_session(cfg, host, dry_run, over)
        hint = host_hint(cfg, ssh_hosts=True)
        # With nothing registered the hint already says to run `sshtsf', so
        # the follow-up would only offer a second, competing suggestion.
        if host_names(cfg):
            hint += ("\n        `sshtsf -c %s` registers it; "
                     "`sshtsf -l` shows the sessions too" % token)
        sys.exit("sshtsf: unknown host or alias: %s\n%s" % (token, hint))

    host = resolve_host(cfg, positional[0])
    if not host:
        unknown_host(cfg, positional[0])
    session = resolve_session(cfg, host, positional[1])
    if session:
        return connect(cfg, host, session, dry_run, over)
    # No such session -- fall into the add-new route with the name filled in.
    print("sshtsf: no session %s on %s" % (positional[1], host), file=sys.stderr)
    return route_add_session(cfg, host, positional[1], dry_run=dry_run, over=over)


def main(argv: list[str] | None = None) -> int:
    """Entry point; argv defaults to sys.argv[1:]. Ctrl-C exits 130, whether
    it lands in a prompt of ours (KeyboardInterrupt) or in click, which
    turns it into an Abort."""
    try:
        return logins.run(cli, argv, "sshtsf")
    except (KeyboardInterrupt, click.Abort):
        print(file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
