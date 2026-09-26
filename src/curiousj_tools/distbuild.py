"""distbuild -- set up builds shared across hosts with distcc.

    distbuild makepkg [-n|--dry-run] [--rust-cpu CPU] [--lto|--no-lto] [TARGET]
    distbuild hosts [-n|--dry-run] [-e|--extra N] [-j|--jobs N] [-l|--local] [--localslots N]
                    [-a TERM]... [-p] [-f FILE]...
    distbuild distccd [-n|--dry-run]
    distbuild sccache-secrets [--secrets FILE] [--force] [-f FILE]...
    distbuild sccache [-n|--dry-run] [--secrets FILE] [-f FILE]...

'makepkg' writes the compiler flags of the system makepkg.conf into the
user's, ~/.config/pacman/makepkg.conf (under XDG_CONFIG_HOME when set), with
the CPU named: "-march=native" becomes "-march=TARGET" in CFLAGS, and
"target-cpu=native" becomes "target-cpu=CPU" in RUSTFLAGS. A distcc host
compiles for its own CPU when told "native", so a package built across
several would be a mix; a named CPU is the same everywhere.

TARGET defaults to what `gcc -march=native` resolves to here, and CPU to
TARGET, or to what rustc resolves "native" to when TARGET is not given.
Whatever -march the system file has is replaced, and an "-mtune=native"
becomes "-mtune=TARGET" alongside it.

--no-lto turns 'lto' off in OPTIONS, --lto back on; without either, OPTIONS
is left alone. With LTO the optimizing and code generation happen when
linking, on this machine, so a distcc host does only the parsing half of a
compile. A PKGBUILD's own options=(lto) or options=(!lto) still wins.

The flags are taken from /etc/makepkg.conf and /etc/makepkg.conf.d/*.conf,
the last setting of each winning as it does for makepkg. An assignment built
on them, such as CXXFLAGS="$CFLAGS ...", is copied as well: the system file
expanded it with the native CFLAGS before the user's file is read. Each is
replaced where the user's file sets it, or added at the end; the rest of the
file is kept as it is. -n prints the change as a diff instead of writing it.

'hosts' points the same file's distcc settings at the logins of the
ssh-lists files that have the 'distccd' attribute (-a replaces that term;
-p and -f as for `ssh-logins`); -l/--local adds this machine as 'localhost'.
It is left out unless asked: it already preprocesses every job and links,
and a share of the compiles on top made builds slower, not faster.
Each gets its nproc plus N (default 4) jobs in DISTCC_HOSTS. distcc takes
a first slot on every host, in list order, before a second on any, so the
limits set each host's share and the order only breaks ties: most cores
first, 'localhost' after its equals, as it preprocesses and links on top.
A login is named by its first address distccd answers on, wired before
wifi (its host name last, which mDNS may well answer with the wifi one).
A --listen in its /etc/conf.d/distccd is the only address distccd
answers on, and is reported when it is the wifi one beside a wired one.
One that cannot be reached over ssh without a prompt, or has no distccd
answering, is left out with a warning.

--localslots N (default 8) ends DISTCC_HOSTS: how many compiles run here at
once when distcc does not distribute them, among them those that failed on
a host and are retried here. distcc's own 4 would leave a build whose
compiles cannot be distributed (C++20 modules, say) at 4 wide, as a failing
host is passed over for a minute and without 'localhost' nothing is left.
N is bounded by memory more than by cores: a C++ compile can take a GB.

MAKEFLAGS and NINJAFLAGS get -j
with every job slot counted, plus this machine's nproc (-j replaces the sum):
a compile beyond the slots only waits for one, but linking, configure checks
and code generation run here and would otherwise leave a slot idle. BUILDENV gets 'distcc' turned on, from the system file
if the user's does not set it. The other lines are kept as they are.
The same DISTCC_HOSTS goes to ~/.distcc/hosts (under DISTCC_DIR when set),
replacing it, for distcc run outside makepkg, which has no MAKEFLAGS of its
own: the -j to use there is printed.

'distccd' runs distccd on this machine as a systemd user unit, as the user
running this, in place of the system unit that runs it as 'distcc': its
settings then live with the user's other files, and changing them needs no
root. The unit is ~/.config/systemd/user/distccd.service (under
XDG_CONFIG_HOME when set) and its DISTCC_ARGS are in ~/.config/distccd.conf,
written once from /etc/conf.d/distccd without --listen, so distccd answers
on every address and starts before the network is up; an existing one is
the user's and kept. Compiles then run as the user, able to read what the
user can; --allow-private admits only machines on private networks.

The one-time root steps are checked first and only those still needed are
run, with sudo, after one password prompt: installing distcc, turning the
system unit off (it holds the port), letting port 3632 through ufw when it
is active, and lingering, so the unit runs with nobody logged in. Then the
unit is enabled and started, or restarted when it changed. -n prints the
files and commands instead.

'sccache-secrets' writes what the sccache-dist hosts share: the scheduler's
URL, the wired address of the login with the 'sccache-scheduler' attribute
(found over ssh as 'hosts' finds its hosts, or this machine), a client token
and the key the servers' tokens are made from. The file is FILE with
--secrets, else $DISTBUILD_SCCACHE_SECRETS, else
~/.config/distbuild/sccache-secrets.toml; an existing one is kept unless
--force. -f names the ssh-lists files, as for `ssh-logins`.

'sccache' sets this machine up for what its entry in the ssh-lists files
says, from the secrets file: with 'sccache-scheduler', the sccache-dist
scheduler (port 10600); with 'sccache-server', a build server (port 10501,
on its wired address); and in any case the client, ~/.config/sccache/config,
with RUSTC_WRAPPER exported in the user's makepkg.conf, so makepkg's rust
builds go through it. The scheduler and server run as systemd user units,
as 'distccd' runs distccd, with the same root steps for sccache, podman and
their ports. A build server runs each compile in a podman container through
sccache-dist's docker builder, with a `docker` that runs podman_docker; the
builder's base image is pulled first. Rust crates that link -- bin, dylib,
cdylib, proc-macro -- are never distributed, nor cached.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import difflib
import os
import pathlib
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tomllib

import click

from . import attrs, cmdline, lists, logins, pick, sshutil
from .lists import Login, ToolError

SYSTEM_CONF = pathlib.Path("/etc/makepkg.conf")
DISTCCD_CONF = "/etc/conf.d/distccd"
UFW_RULES = "/etc/ufw/user.rules"
USER_UNIT = """\
[Unit]
Description=Distributed C, C++ and Objective-C compiler, as the user
Documentation=man:distccd(1)

[Service]
EnvironmentFile={args}
ExecStart=/usr/bin/distccd --no-detach --daemon $DISTCC_ARGS
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
DISTCCD_PORT = 3632
SCHEDULER_PORT = 10600
BUILD_SERVER_PORT = 10501
BASE_IMAGE = "docker.io/aidanhs/busybox"
SCCACHE_DIST_UNIT = """\
[Unit]
Description=sccache-dist {role}, as the user
Documentation=https://github.com/mozilla/sccache/blob/main/docs/Distributed.md

[Service]
Environment=SCCACHE_NO_DAEMON=1
{env}ExecStart=/usr/bin/sccache-dist {role} --config {conf}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
DEFAULT_TERM = "distccd"

_NAME = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=", re.M)
# A flag value stops at a closing quote or a line continuation, not only at a space.
_VALUE = r"""[^\s"'\\]+"""


def user_conf() -> pathlib.Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return pathlib.Path(config_home) / "pacman" / "makepkg.conf"


def system_confs(conf: pathlib.Path = SYSTEM_CONF) -> list[pathlib.Path]:
    """The files makepkg reads before the user's, in its order."""
    return [conf] + sorted(pathlib.Path(f"{conf}.d").glob("*.conf"))


@dataclasses.dataclass
class Assignment:
    name: str
    start: int
    end: int
    text: str


def _line_end(text: str, i: int) -> int:
    """Where the logical line through i ends: at the first newline outside
    quotes, parentheses and backslash continuations. A comment ends it too."""
    quote, depth = None, 0
    while i < len(text):
        c = text[i]
        if quote == "'":
            if c == "'":
                quote = None
        elif c == "\\":
            i += 1
        elif quote == '"':
            if c == '"':
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "#" and (i == 0 or text[i - 1] in " \t\n"):
            nl = text.find("\n", i)
            i = len(text) if nl < 0 else nl
            continue
        elif c == "\n" and depth <= 0:
            return i
        i += 1
    return len(text)


def assignments(text: str) -> list[Assignment]:
    """The shell variable assignments in text, one per logical line, in order.
    A line inside a multi-line value is part of that value, not one of its own."""
    found, pos = [], 0
    while (m := _NAME.search(text, pos)):
        end = _line_end(text, m.end())
        found.append(Assignment(m.group(1), m.start(), end, text[m.start():end]))
        pos = end
    return found


def _references(text: str, name: str) -> bool:
    value = text.split("=", 1)[1]
    return re.search(rf"\$\{{?{name}\b", value) is not None


@dataclasses.dataclass
class Override:
    name: str
    text: str
    note: str  # the comment over it when it is added at the end


def overrides(confs: list[tuple[pathlib.Path, str]], march: str, rust_cpu: str) -> list[Override]:
    """The assignments to put in the user's file, from the system files' text:
    the last CFLAGS and RUSTFLAGS with the CPU named, and each assignment
    built on either of them. One the system files do not set is left out."""
    subs = [(re.compile(rf"-march={_VALUE}"), f"-march={march}"),
            (re.compile(r"-mtune=native\b"), f"-mtune={march}"),
            (re.compile(rf"target-cpu={_VALUE}"), f"target-cpu={rust_cpu}")]
    last: dict[str, Override] = {}
    for path, text in confs:
        for a in assignments(text):
            last.pop(a.name, None)  # re-inserted, so the order is the last setting's
            last[a.name] = Override(a.name, a.text, f"from {path}")
    picked = []
    for key in ("CFLAGS", "RUSTFLAGS"):
        if key in last:
            picked.append(last[key])
            picked += [o for o in last.values() if o.name != key and _references(o.text, key)]
    for o in picked:
        for pattern, replacement in subs:
            o.text = pattern.sub(replacement, o.text)
    return picked


def apply(user_text: str, wanted: list[Override]) -> str:
    """user_text with each wanted assignment in place of its last setting
    there. One it lacks follows the one before it with the same note, as
    CXXFLAGS follows CFLAGS; failing that it is added at the end, under
    its note."""
    text = user_text
    appended: list[Override] = []
    after, prev = None, None  # where the previous one ended in text, and it
    for o in wanted:
        mine = [a for a in assignments(text) if a.name == o.name]
        if mine:
            a = mine[-1]
            text = text[:a.start] + o.text + text[a.end:]
            after = a.start + len(o.text)
        elif after is not None and prev.note == o.note:
            text = text[:after] + "\n" + o.text + text[after:]
            after += 1 + len(o.text)
        else:
            appended.append(o)
            after = None
        prev = o
    note = None
    for o in appended:
        if text and not text.endswith("\n"):
            text += "\n"
        if o.note != note:
            text += ("" if not text or text.endswith("\n\n") else "\n") + f"#-- {o.note}\n"
            note = o.note
        text += o.text + "\n"
    return text


@dataclasses.dataclass
class Helper:
    host: str  # as DISTCC_HOSTS names it
    nproc: int
    local: bool = False


def listen_address(distccd_conf: str) -> str | None:
    """The address DISTCC_ARGS has distccd --listen on, if it names one."""
    args = [a for a in assignments(distccd_conf) if a.name == "DISTCC_ARGS"]
    m = re.search(r"--listen[= ]+([^\s\"']+)", args[-1].text) if args else None
    return m.group(1) if m else None


def distccd_answers(host: str) -> bool:
    try:
        with socket.create_connection((host, DISTCCD_PORT), timeout=sshutil.PROBE_CONNECT_TIMEOUT):
            return True
    except OSError:
        return False


_MARK = "--distbuild--"
# Run on the login by sh, whatever its login shell: nproc, the distccd
# config, then "wired|wifi ADDRESS" for each IPv4 address of a physical
# interface.
PROBE_SCRIPT = f"""\
nproc; echo {_MARK}
cat {DISTCCD_CONF} 2>/dev/null; echo {_MARK}
for d in /sys/class/net/*; do
    [ -e "$d/device" ] || continue
    if [ -e "$d/wireless" ] || [ -e "$d/phy80211" ]; then kind=wifi; else kind=wired; fi
    ip -o -4 addr show dev "${{d##*/}}" 2>/dev/null |
        while read -r _ _ _ addr _; do echo "$kind ${{addr%/*}}"; done
done
"""


@dataclasses.dataclass
class Probed:
    """What PROBE_SCRIPT found on a login."""
    nproc: int
    listen: str | None
    wired: list[str]
    wifi: list[str]


def parse_probe(out: str) -> Probed | None:
    parts = out.split(f"{_MARK}\n")
    if len(parts) != 3 or not parts[0].strip().isdigit():
        return None
    addrs = [line.split() for line in parts[2].splitlines() if len(line.split()) == 2]
    return Probed(int(parts[0]), listen_address(parts[1]),
                  [a for kind, a in addrs if kind == "wired"],
                  [a for kind, a in addrs if kind == "wifi"])


def candidates(found: Probed, name: str) -> tuple[list[str], str | None]:
    """The addresses to try for distccd, wired first, and a warning when it
    listens on wifi only while a wired address is there. A --listen address
    is the only one distccd answers on; without one, the host name comes
    last, for an address the interfaces did not show."""
    if found.listen:
        why = None
        if found.listen in found.wifi and found.wired:
            why = (f"distccd listens on wifi {found.listen} only; --listen {found.wired[0]} "
                   f"in its {DISTCCD_CONF} would use the wired one")
        return [found.listen], why
    return found.wired + found.wifi + [name], None


def probe(login: Login) -> tuple[Helper | None, list[str]]:
    """login as a distcc helper, or None, and the warnings on the way."""
    proc = subprocess.run(
        sshutil.probe_ssh(login.login, batch=True) + ["sh", "-c", shlex.quote(PROBE_SCRIPT)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL)
    found = parse_probe(proc.stdout) if proc.returncode == 0 else None
    if found is None:
        why = proc.stderr.strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
        return None, [f"{login.login}: cannot ask for its nproc without a prompt ({why[0]}); left out"]
    name = sshutil.ssh_config(login.login).get("hostname") or login.login.rpartition("@")[2]
    tried, why = candidates(found, name)
    warnings = [f"{login.login}: {why}"] if why else []
    for host in tried:
        if distccd_answers(host):
            return Helper(host, found.nproc), warnings
    return None, warnings + [f"{login.login}: no distccd answering on "
                             f"{' or '.join(tried)}, port {DISTCCD_PORT}; left out"]


def local_nproc() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:  # macOS
        return os.cpu_count() or 1


def distcc_hosts(helpers: list[Helper], extra: int, localslots: int) -> str:
    """DISTCC_HOSTS for helpers, most cores first, 'localhost' after its equals,
    then --localslots. The same address twice (two logins on one machine)
    counts once."""
    seen, line = set(), []
    for h in sorted(helpers, key=lambda h: (-h.nproc, h.local)):
        if h.host not in seen:
            seen.add(h.host)
            line.append(f"{h.host}/{h.nproc + extra}")
    return " ".join(line + [f"--localslots={localslots}"])


def set_option(array: str, word: str, on: bool) -> str:
    """An OPTIONS=(...) or BUILDENV=(...) assignment with word on, or off
    ("!word"), added at the front when the array lacks it either way."""
    have = re.compile(rf"(?<![\w!-])!?{re.escape(word)}(?![\w-])")
    want = word if on else f"!{word}"
    if have.search(array):
        return have.sub(want, array)
    return array.replace("(", f"({want} ", 1)


def last_array(user_text: str, confs: list[tuple[pathlib.Path, str]], name: str) -> str:
    """The user's setting of the array name, else the system files' last."""
    return ([a.text for a in assignments(user_text) if a.name == name]
            or [a.text for _, text in confs for a in assignments(text) if a.name == name]
            or [f"{name}=()"])[-1]


def slots(hosts: str) -> int:
    """The jobs a DISTCC_HOSTS line allows at once on its hosts; --localslots
    and the like are not hosts."""
    return sum(int(spec.rpartition("/")[2]) for spec in hosts.split() if not spec.startswith("--"))


def host_overrides(user_text: str, confs: list[tuple[pathlib.Path, str]],
                   hosts: str, jobs: int) -> list[Override]:
    """MAKEFLAGS and NINJAFLAGS with -j jobs, BUILDENV and DISTCC_HOSTS for
    the given hosts line, in the order the system file has them."""
    note = "set by `distbuild hosts`"
    return [Override("MAKEFLAGS", f'MAKEFLAGS="-j{jobs}"', note),
            Override("NINJAFLAGS", f'NINJAFLAGS="-j{jobs}"', note),
            Override("BUILDENV", set_option(last_array(user_text, confs, "BUILDENV"), "distcc", True), note),
            Override("DISTCC_HOSTS", f'DISTCC_HOSTS="{hosts}"', note)]


def native_left(user_text: str, confs: list[tuple[pathlib.Path, str]]) -> bool:
    """Whether CFLAGS still say -march=native once the user's file is read."""
    cflags = [a.text for _, text in confs + [(None, user_text)]
              for a in assignments(text) if a.name == "CFLAGS"]
    return bool(cflags) and "-march=native" in cflags[-1]


def native_march() -> str | None:
    """What `gcc -march=native` means on this machine, if gcc says."""
    try:
        out = subprocess.run(["gcc", "-march=native", "-Q", "--help=target"],
                             capture_output=True, text=True).stdout
    except OSError:
        return None
    m = re.search(r"^\s*-march=\s+(\S+)", out, re.M)
    return m.group(1) if m else None


def native_rust_cpu() -> str | None:
    """What rustc's target-cpu=native means on this machine, if rustc says."""
    try:
        out = subprocess.run(["rustc", "--print", "target-cpus"],
                             capture_output=True, text=True).stdout
    except OSError:
        return None
    m = re.search(r"^\s*native\b.*\(currently (\S+?)\)", out, re.M)
    return m.group(1) if m else None


@click.group(cls=cmdline.Group, help=__doc__)
def cli() -> None:
    pass


@cli.command(cls=cmdline.Command, help=__doc__)
@click.option("-n", "--dry-run", is_flag=True, help="Print the change as a diff; write nothing.")
@click.option("--rust-cpu", metavar="CPU", help="The target-cpu for RUSTFLAGS; default TARGET.")
@click.option("--lto/--no-lto", default=None, help="Turn 'lto' on or off in OPTIONS; default leave it.")
@click.argument("target", required=False)
def makepkg(target: str | None, rust_cpu: str | None, lto: bool | None, dry_run: bool) -> int:
    march = target or native_march()
    if not march:
        raise click.ClickException("cannot tell what -march=native is here; name the TARGET")
    rust_cpu = rust_cpu or (target if target else native_rust_cpu() or march)

    path, old, confs = read_confs()
    wanted = overrides(confs, march, rust_cpu)
    if not any(o.name == "CFLAGS" for o in wanted):
        raise click.ClickException(f"no CFLAGS in {SYSTEM_CONF}")
    what = f"{', '.join(o.name for o in wanted)} with -march={march}, target-cpu={rust_cpu}"
    if lto is not None:
        options = set_option(last_array(old, confs, "OPTIONS"), "lto", lto)
        wanted.append(Override("OPTIONS", options, "set by `distbuild makepkg`"))
        what += ", OPTIONS with " + ("lto" if lto else "!lto")
    return write(path, old, apply(old, wanted), dry_run, what)


@cli.command(cls=cmdline.Command, help=__doc__)
@click.option("-n", "--dry-run", is_flag=True, help="Print the change as a diff; write nothing.")
@click.option("-e", "--extra", metavar="N", type=click.IntRange(min=0), default=4, show_default=True,
              help="Jobs per host on top of its nproc.")
@click.option("-j", "--jobs", metavar="N", type=click.IntRange(min=1),
              help="MAKEFLAGS and NINJAFLAGS -j; default the slots plus this machine's nproc.")
@click.option("-l", "--local", is_flag=True, help="Build on this machine too, as localhost.")
@click.option("--localslots", metavar="N", type=click.IntRange(min=1), default=8, show_default=True,
              help="Compiles run here at once when not distributed.")
@logins.login_options(no_local=False)
def hosts(opts: logins.LoginOpts, extra: int, jobs: int | None, local: bool, localslots: int,
          dry_run: bool) -> int:
    path, old, confs = read_confs()
    # localhost is added here with --local, whatever its attributes: makepkg runs on it.
    remote_opts = dataclasses.replace(
        opts, no_local=True, terms=opts.terms or (attrs.term(DEFAULT_TERM, "term"),))
    try:
        # A login with a via is a place inside a machine, not one of its own.
        remote = [e for e in logins.select(remote_opts) if e.via is None]
    except ToolError as e:
        raise click.ClickException(str(e))
    except pick.Abort:
        return pick.EXIT_ABORT

    with concurrent.futures.ThreadPoolExecutor() as pool:
        probed = list(pool.map(probe, remote))
    helpers = [Helper("localhost", local_nproc(), local=True)] if local else []
    for helper, warnings in probed:
        for w in warnings:
            click.echo(f"distbuild: {w}", err=True)
        if helper:
            helpers.append(helper)
    if not helpers:
        raise click.ClickException("no hosts to build on")
    if native_left(old, confs):
        click.echo("distbuild: CFLAGS still say -march=native, which each host reads as its own "
                   "CPU; run `distbuild makepkg` too", err=True)

    line = distcc_hosts(helpers, extra, localslots)
    jobs = jobs or slots(line) + local_nproc()
    write(path, old, apply(old, host_overrides(old, confs, line, jobs)), dry_run,
          f'DISTCC_HOSTS="{line}", -j{jobs}')
    hosts_file = distcc_hosts_file()
    old_hosts = hosts_file.read_text() if hosts_file.exists() else ""
    return write(hosts_file, old_hosts, hosts_file_text(line, path), dry_run,
                 f"the same, for distcc outside makepkg: -j{jobs} there")


def distcc_hosts_file() -> pathlib.Path:
    """Where distcc looks for its hosts when DISTCC_HOSTS is unset."""
    return pathlib.Path(os.environ.get("DISTCC_DIR") or os.path.expanduser("~/.distcc")) / "hosts"


def hosts_file_text(line: str, makepkg_conf: pathlib.Path) -> str:
    return (f"# written by `distbuild hosts`, as DISTCC_HOSTS in {makepkg_conf},\n"
            f"# which makepkg exports in place of this file\n{line}\n")


@cli.command(cls=cmdline.Command, help=__doc__)
@click.option("-n", "--dry-run", is_flag=True, help="Print the files and commands; change nothing.")
def distccd(dry_run: bool) -> int:
    args_path = config_home() / "distccd.conf"
    unit_path = config_home() / "systemd" / "user" / "distccd.service"

    writes = []
    if not args_path.exists():
        try:
            system = pathlib.Path(DISTCCD_CONF).read_text()
        except OSError:
            system = ""
        writes.append(Write(args_path, f'DISTCC_ARGS="{distccd_args(system)}"\n'))
    unit_w = changed(unit_path, USER_UNIT.format(args=args_path))
    if unit_w:
        writes.append(unit_w)

    steps = root_steps(sshutil.login_name(), ["distcc"], ["distccd.service"], [DISTCCD_PORT])
    done = carry_out(writes, steps, [], [("distccd.service", bool(writes))], dry_run)
    if not dry_run:
        click.echo(f"distccd runs as {sshutil.login_name()}" + ("" if done else " (unchanged)"))
    return 0

def distccd_args(system_conf: str) -> str:
    """DISTCC_ARGS for the user unit: the system file's without --listen, or
    --allow-private when that leaves none (distccd admits no one by default)."""
    found = [a for a in assignments(system_conf) if a.name == "DISTCC_ARGS"]
    words = " ".join(shlex.split(found[-1].text.split("=", 1)[1])).split() if found else []
    kept, skip = [], False
    for word in words:
        if skip:
            skip = False
        elif word == "--listen":
            skip = True
        elif not word.startswith("--listen="):
            kept.append(word)
    return " ".join(kept) or "--allow-private"


def output(argv: list[str]) -> str:
    """argv's stdout, stripped; what it printed even when it failed, as
    `systemctl is-enabled` does for "disabled". "" when it cannot run."""
    try:
        return subprocess.run(argv, capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def run(argv: list[str]) -> int:
    """argv on this terminal, so sudo and pacman can ask."""
    try:
        return subprocess.run(argv).returncode
    except OSError:
        return 127


def ufw_allows(port: int) -> bool | None:
    """Whether ufw's rules let TCP port in; None when they cannot be read."""
    try:
        rules = pathlib.Path(UFW_RULES).read_text()
    except OSError:
        return None
    return re.search(rf"^### tuple ### allow tcp {port} ", rules, re.M) is not None


def root_steps(user: str, packages: list[str], system_units: list[str], ports: list[int],
               linger: bool = True) -> list[tuple[str, list[str]]]:
    """(why, command) for each one-time root step still needed, in the order
    they have to run: a system unit holding a port is off before the user
    unit wanting it starts."""
    steps = []
    missing = [pkg for pkg in packages if not output(["pacman", "-Q", pkg])]
    if missing:
        steps.append((f"{', '.join(missing)} not installed",
                      ["sudo", "pacman", "-S", "--needed", *missing]))
    for unit in system_units:
        if (output(["systemctl", "is-enabled", unit]) == "enabled"
                or output(["systemctl", "is-active", unit]) == "active"):
            steps.append((f"the system {unit.removesuffix('.service')} holds the port",
                          ["sudo", "systemctl", "disable", "--now", unit]))
    if output(["systemctl", "is-active", "ufw.service"]) == "active":
        for port in ports:
            if not ufw_allows(port):
                steps.append(("ufw is on", ["sudo", "ufw", "allow", f"{port}/tcp"]))
    if linger and output(["loginctl", "show-user", user, "-p", "Linger", "--value"]) != "yes":
        steps.append(("the units should run with nobody logged in",
                      ["sudo", "loginctl", "enable-linger", user]))
    return steps


@dataclasses.dataclass
class Write:
    path: pathlib.Path
    text: str
    mode: int = 0o644  # 0o600 for a file holding a secret, 0o755 for a script


def carry_out(writes: list[Write], steps: list[tuple[str, list[str]]],
              commands: list[tuple[list[str], bool]], units: list[tuple[str, bool]],
              dry_run: bool) -> bool:
    """The root steps, after one sudo prompt; the writes; the commands, each
    (argv, whether it has to succeed); then each user unit enabled and
    running, restarted when (unit, changed) says it changed. With dry_run,
    print them instead. Whether anything was to be done."""
    user = [["systemctl", "--user", "daemon-reload"]] if any(c for _, c in units) else []
    for unit, changed in units:
        running = output(["systemctl", "--user", "is-active", unit]) == "active"
        if output(["systemctl", "--user", "is-enabled", unit]) != "enabled" or not running:
            user.append(["systemctl", "--user", "enable", "--now", unit])
        if running and changed:
            user.append(["systemctl", "--user", "restart", unit])
    todo = bool(writes or steps or commands or user)

    if dry_run:
        for w in writes:
            try:
                old = w.path.read_text()
            except OSError:
                click.echo(f"--- {w.path}\n{w.text}", nl=False)
                continue
            sys.stdout.writelines(difflib.unified_diff(
                old.splitlines(keepends=True), w.text.splitlines(keepends=True), str(w.path), str(w.path)))
        for why, argv in steps:
            click.echo(f"{shlex.join(argv)}    # {why}")
        for argv in [a for a, _ in commands] + user:
            click.echo(shlex.join(argv))
        return todo

    if steps:
        for why, argv in steps:
            click.echo(f"distbuild: {why}: {shlex.join(argv)}", err=True)
        if run(["sudo", "-v"]) != 0:
            raise click.ClickException("sudo refused; nothing changed")
        for why, argv in steps:
            if run(argv) != 0:
                raise click.ClickException(f"failed: {shlex.join(argv)}")
    for w in writes:
        w.path.parent.mkdir(parents=True, exist_ok=True)
        w.path.write_text(w.text)
        w.path.chmod(w.mode)
        click.echo(f"wrote {w.path}")
    for argv, must in commands + [(a, True) for a in user]:
        if run(argv) != 0 and must:
            raise click.ClickException(f"failed: {shlex.join(argv)}")
    for unit, _ in units:
        state = output(["systemctl", "--user", "is-active", unit])
        if state != "active":
            name = unit.removesuffix(".service")
            raise click.ClickException(f"{name} is {state or 'not running'}; "
                                       f"see `journalctl --user -u {name}`")
    return todo


def config_home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"))


def changed(path: pathlib.Path, text: str) -> Write | None:
    """A Write of text to path, or None when path holds it already."""
    try:
        if path.read_text() == text:
            return None
    except OSError:
        pass
    return Write(path, text)


@dataclasses.dataclass
class Secrets:
    scheduler_url: str
    client_token: str
    server_key: str


def secrets_file(given: str | None) -> pathlib.Path:
    return pathlib.Path(os.path.expanduser(
        given or os.environ.get("DISTBUILD_SCCACHE_SECRETS")
        or str(config_home() / "distbuild" / "sccache-secrets.toml")))


def secrets_text(s: Secrets) -> str:
    return (f"# written by `distbuild sccache-secrets`: what the sccache-dist hosts share\n"
            f'scheduler_url = "{s.scheduler_url}"\n'
            f'client_token = "{s.client_token}"\n'
            f'server_key = "{s.server_key}"\n')


def read_secrets(path: pathlib.Path) -> Secrets:
    try:
        data = tomllib.loads(path.read_text())
    except OSError:
        raise click.ClickException(f"no {path}; write it with `distbuild sccache-secrets`")
    except tomllib.TOMLDecodeError as e:
        raise click.ClickException(f"{path}: {e}")
    try:
        return Secrets(data["scheduler_url"], data["client_token"], data["server_key"])
    except KeyError as e:
        raise click.ClickException(f"{path}: no {e.args[0]}")


def local_addresses() -> Probed:
    """This machine's addresses, from the probe the logins get over ssh."""
    out = subprocess.run(["sh", "-c", PROBE_SCRIPT], capture_output=True, text=True).stdout
    found = parse_probe(out)
    if found is None:
        raise click.ClickException("cannot list this machine's addresses")
    return found


def login_address(login: Login) -> str:
    """The address to reach login's services at: its first wired address,
    else its first wifi one, else its host name."""
    if login.login == "localhost":
        found = local_addresses()
    else:
        proc = subprocess.run(sshutil.probe_ssh(login.login, batch=True)
                              + ["sh", "-c", shlex.quote(PROBE_SCRIPT)],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)
        found = parse_probe(proc.stdout) if proc.returncode == 0 else None
        if found is None:
            raise click.ClickException(f"{login.login}: cannot ask for its addresses")
    return (found.wired + found.wifi)[0] if found.wired or found.wifi \
        else login.login.rpartition("@")[2]


def this_machine(files: tuple[str, ...]) -> Login:
    """'localhost' as the ssh-lists files describe this machine."""
    try:
        found = lists.load_all(files)
    except ToolError as e:
        raise click.ClickException(str(e))
    return logins.local_login(found.logins, logins.self_names(), sshutil.login_name())


def sccache_dist(*args: str) -> str:
    if not shutil.which("sccache-dist"):
        raise click.ClickException("no sccache-dist; install sccache first (`sudo pacman -S sccache`)")
    out = output(["sccache-dist", *args])
    if not out:
        raise click.ClickException(f"`sccache-dist {' '.join(args[:2])}` gave nothing")
    return out


@cli.command("sccache-secrets", cls=cmdline.Command, help=__doc__)
@click.option("--secrets", "secrets_path", metavar="FILE", help="The secrets file to write.")
@click.option("--force", is_flag=True, help="Replace an existing one.")
@click.option("-f", "files", metavar="FILE", multiple=True,
              help="an ssh-lists file to read (repeatable; these and no other)")
def sccache_secrets(secrets_path: str | None, force: bool, files: tuple[str, ...]) -> int:
    path = secrets_file(secrets_path)
    if path.exists() and not force:
        raise click.ClickException(f"{path} exists; --force replaces it, and every host's setup")
    opts = logins.LoginOpts(files=files, terms=(attrs.term("sccache-scheduler", "term"),))
    try:
        found = logins.select(opts)
    except ToolError as e:
        raise click.ClickException(str(e))
    if len(found) != 1:
        raise click.ClickException("more than one login has 'sccache-scheduler': "
                                   + ", ".join(e.login for e in found))
    s = Secrets(f"http://{login_address(found[0])}:{SCHEDULER_PORT}",
                secrets.token_urlsafe(32), sccache_dist("auth", "generate-jwt-hs256-key"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secrets_text(s))
    path.chmod(0o600)
    click.echo(f"{path}: scheduler {s.scheduler_url}")
    return 0


@cli.command(cls=cmdline.Command, help=__doc__)
@click.option("-n", "--dry-run", is_flag=True, help="Print the files and commands; change nothing.")
@click.option("--secrets", "secrets_path", metavar="FILE", help="The secrets file to read.")
@click.option("-f", "files", metavar="FILE", multiple=True,
              help="an ssh-lists file to read (repeatable; these and no other)")
def sccache(dry_run: bool, secrets_path: str | None, files: tuple[str, ...]) -> int:
    s = read_secrets(secrets_file(secrets_path))
    me = this_machine(files)
    scheduler = "sccache-scheduler" in me.attributes
    server = "sccache-server" in me.attributes
    conf_dir = config_home() / "sccache-dist"
    unit_dir = config_home() / "systemd" / "user"
    writes: list[Write | None] = []
    units: list[tuple[str, bool]] = []
    commands: list[tuple[list[str], bool]] = []

    if scheduler:
        conf = conf_dir / "scheduler.conf"
        conf_w = changed(conf, scheduler_conf(s))
        unit_w = changed(unit_dir / "sccache-dist-scheduler.service",
                         SCCACHE_DIST_UNIT.format(role="scheduler", env="", conf=conf))
        writes += [conf_w and Write(conf_w.path, conf_w.text, 0o600), unit_w]
        units.append(("sccache-dist-scheduler.service", bool(conf_w or unit_w)))
    if server:
        addr = local_addresses()
        public = f"{(addr.wired + addr.wifi or ['127.0.0.1'])[0]}:{BUILD_SERVER_PORT}"
        shim_dir = pathlib.Path(os.path.expanduser("~/.local/share/distbuild/podman-docker"))
        conf = conf_dir / "server.conf"
        token = sccache_dist("auth", "generate-jwt-hs256-server-token",
                             "--secret-key", s.server_key, "--server", public)
        cache = pathlib.Path(os.path.expanduser("~/.cache/sccache-dist"))
        conf_w = changed(conf, server_conf(s, public, token, cache))
        shim_w = changed(shim_dir / "docker",
                         f'#!/bin/sh\nexec {sys.executable} -m curiousj_tools.podman_docker "$@"\n')
        unit_w = changed(unit_dir / "sccache-dist-server.service",
                         SCCACHE_DIST_UNIT.format(role="server", conf=conf,
                                                  env=f"Environment=PATH={shim_dir}:/usr/bin:/bin\n"))
        writes += [conf_w and Write(conf_w.path, conf_w.text, 0o600),
                   shim_w and Write(shim_w.path, shim_w.text, 0o755), unit_w]
        units.append(("sccache-dist-server.service", bool(conf_w or shim_w or unit_w)))
        if run_quiet(["podman", "image", "exists", BASE_IMAGE]) != 0:
            commands.append((["podman", "pull", "-q", BASE_IMAGE], True))

    client = config_home() / "sccache" / "config"
    if client.exists() and not client.read_text().startswith(CLIENT_HEADER):
        raise click.ClickException(f"{client} is not `distbuild sccache`'s; "
                                   "add its [dist] by hand, or move it away")
    client_w = changed(client, client_conf(s))
    if client_w:
        writes.append(Write(client_w.path, client_w.text, 0o600))
        # A running sccache read the old one; the next compile starts a new one.
        commands.append((["sccache", "--stop-server"], False))
    path, old, confs = read_confs()
    wrapper = Override("RUSTC_WRAPPER", "export RUSTC_WRAPPER=/usr/bin/sccache",
                       "set by `distbuild sccache`")
    new = apply(old, [wrapper])
    if new != old:
        writes.append(Write(path, new))

    ports = [SCHEDULER_PORT] * scheduler + [BUILD_SERVER_PORT] * server
    system_units = ["sccache-scheduler.service"] * scheduler + ["sccache-server.service"] * server
    steps = root_steps(sshutil.login_name(), ["sccache"] + ["podman"] * server, system_units, ports,
                       linger=scheduler or server)
    done = carry_out([w for w in writes if w], steps, commands, units, dry_run)
    if not dry_run:
        roles = ["scheduler"] * scheduler + ["build server"] * server + ["client"]
        click.echo(f"sccache-dist {', '.join(roles)} for {s.scheduler_url}"
                   + ("" if done else " (unchanged)"))
    return 0


CLIENT_HEADER = "# written by `distbuild sccache`"


def client_conf(s: Secrets) -> str:
    return (f"{CLIENT_HEADER}, from the sccache-dist secrets\n"
            f"[dist]\n"
            f'scheduler_url = "{s.scheduler_url}"\n'
            f"toolchains = []\n\n"
            f"[dist.auth]\n"
            f'type = "token"\n'
            f'token = "{s.client_token}"\n')


def scheduler_conf(s: Secrets) -> str:
    return (f'public_addr = "0.0.0.0:{SCHEDULER_PORT}"\n\n'
            f"[client_auth]\n"
            f'type = "token"\n'
            f'token = "{s.client_token}"\n\n'
            f"[server_auth]\n"
            f'type = "jwt_hs256"\n'
            f'secret_key = "{s.server_key}"\n')


def server_conf(s: Secrets, public: str, token: str, cache: pathlib.Path) -> str:
    return (f'cache_dir = "{cache}"\n'
            f'public_addr = "{public}"\n'
            f'scheduler_url = "{s.scheduler_url}"\n\n'
            f"[builder]\n"
            f'type = "docker"\n\n'
            f"[scheduler_auth]\n"
            f'type = "jwt_token"\n'
            f'token = "{token}"\n')


def run_quiet(argv: list[str]) -> int:
    try:
        return subprocess.run(argv, capture_output=True).returncode
    except OSError:
        return 127


def read_confs() -> tuple[pathlib.Path, str, list[tuple[pathlib.Path, str]]]:
    """The user's makepkg.conf, its text ("" while missing) and the system files'."""
    path = user_conf()
    legacy = pathlib.Path(os.path.expanduser("~/.makepkg.conf"))
    if not path.exists() and legacy.exists():
        # makepkg reads the legacy file only while the XDG one is missing.
        raise click.ClickException(f"makepkg reads {legacy}; move it to {path} first")
    confs = []
    for conf in system_confs(SYSTEM_CONF):
        try:
            confs.append((conf, conf.read_text()))
        except OSError as e:
            raise click.ClickException(f"cannot read {conf}: {e.strerror}")
    return path, (path.read_text() if path.exists() else ""), confs


def write(path: pathlib.Path, old: str, new: str, dry_run: bool, what: str) -> int:
    """new to path, or with dry_run the change as a diff; what says what it set."""
    if dry_run:
        sys.stdout.writelines(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            str(path) if old else "/dev/null", str(path)))
        return 0
    if new != old:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new)
    click.echo(f"{path}: {what}" + ("" if new != old else " (unchanged)"))
    return 0


def main(argv: list[str] | None = None) -> int:
    return cmdline.run(cli, argv, "distbuild")


if __name__ == "__main__":
    sys.exit(main())
