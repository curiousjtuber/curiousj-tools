"""ssh-logins -- print the login list xssh and pssh work from, optionally picking a subset.

    ssh-logins [-h] [-N|--no-local] [-a TERM]... [-p|--pick] [-f FILE]...

One entry per line: every login ([user@]host) in the lists files, then
`localhost` last, which consumers turn into whatever "this machine" means
for them (xssh: a local pane, pssh: a local run); -N/--no-local leaves it
out. Entries that name this machine, for this user, are dropped, those
with a via too, so one list can serve every host on it; `localhost` takes
over the attributes and operations of the first such entry without a via.
-p/--pick shows the list in fzf (TAB marks several) and prints only the
marked entries; without fzf, a numbered menu (see `pick-lines -h`).
Aborting the picker exits 130 with nothing printed.

-a/--attr TERM keeps the logins whose attributes satisfy TERM: `mise` has
it, `arch=x86_64` has it with that value, `!mise` lacks it, `arch!=x86_64`
lacks it or has another value; given several times, every TERM has to hold.
`localhost` is kept or dropped like any other, by the attributes it took
over; with no entry naming this machine it has none. Quote a `!` for the
shell.

The logins are the `logins` list of the ssh-lists files, TOML, YAML or JSON
(see the README, or the curiousj_tools.lists docstring, for the shape).
Which files, merged in this order:

    -f FILE                         on the command line, repeatable
    $SSH_LISTS_FILE                 colon-separated files, without -f
    DIR/ssh-lists*.toml|yaml|yml|json   otherwise, for each DIR in $SSH_LISTS_PATH
                                    (colon-separated), default ~/.config
"""

from __future__ import annotations

import functools
import getpass
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass

import click

from . import attrs, lists, pick
from .lists import Login, ToolError  # noqa: F401  (re-exported)

EXIT_ERROR = 1
EXIT_USAGE = 2


@dataclass
class LoginOpts:
    """The login-selection flags xssh and pssh share with ssh-logins."""

    no_local: bool = False
    pick: bool = False
    files: tuple[str, ...] = ()
    attrs: tuple[attrs.Term, ...] = ()


def attr_terms(ctx, param, value) -> tuple[attrs.Term, ...]:
    """A repeatable TERM option's values parsed; a bad one is a usage error."""
    try:
        return tuple(attrs.term(t, "term") for t in value)
    except ValueError as e:
        raise click.BadParameter(str(e).removeprefix("term: "))


def login_options(f):
    """The -N, -a, -p and -f every login tool takes; the callback gets them
    as one LoginOpts named opts."""
    @click.option("-f", "files", metavar="FILE", multiple=True,
                  help="an ssh-lists file to read (repeatable; these and no other)")
    @click.option("-p", "--pick", is_flag=True, help="choose logins interactively")
    @click.option("-a", "--attr", "attrs", metavar="TERM", multiple=True, callback=attr_terms,
                  help="keep logins with the attribute: key, key=value, !key, key!=value (repeatable)")
    @click.option("-N", "--no-local", is_flag=True, help="leave localhost out")
    @functools.wraps(f)
    def wrapper(no_local, attrs, pick, files, **kw):
        return f(LoginOpts(no_local, pick, tuple(files), attrs), **kw)
    return wrapper


class Command(click.Command):
    """A command whose help is its module docstring as written -- the usage
    lines and file excerpts there are laid out by hand -- with the option
    summary after it. -h works like --help."""

    context_settings = {"help_option_names": ["-h", "--help"]}

    def __init__(self, *args, **kw):
        settings = dict(self.context_settings, **(kw.pop("context_settings", None) or {}))
        super().__init__(*args, context_settings=settings, **kw)

    def format_help(self, ctx, formatter):
        formatter.write((self.help or "").rstrip() + "\n")
        self.format_options(ctx, formatter)


def run(command: click.Command, argv: list[str] | None, prog: str) -> int:
    """command as a main(): its return value as the exit status, a usage
    error printed and 2, --help printed and 0."""
    try:
        rv = command.main(args=argv, prog_name=prog, standalone_mode=False)
    except click.UsageError as e:
        e.show()
        return EXIT_USAGE
    except click.ClickException as e:
        e.show()
        return e.exit_code
    return rv if isinstance(rv, int) else 0


def self_names() -> set[str]:
    """Lowercased names this machine answers to: hostname, its first label,
    the loopback names and, on macOS, the Bonjour name (`my-mac.local` for a
    host called MyMac)."""
    names = {"localhost", "127.0.0.1", "::1"}
    for name in (socket.gethostname(), os.uname().nodename):
        if name:
            names.add(name.lower())
            names.add(name.lower().split(".", 1)[0])
    if shutil.which("scutil"):
        proc = subprocess.run(["scutil", "--get", "LocalHostName"],
                              capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            names.add(proc.stdout.strip().lower())
    return names


def is_self(entry: Login, names: set[str], user: str) -> bool:
    """Whether the entry names this machine, for this user. An entry for
    another user on this machine is a different login, not this one."""
    entry_user, _, host = entry.login.rpartition("@")
    host = host.lower()
    mine = host in names or host.split(".", 1)[0] in names
    return mine and (not entry_user or entry_user == user)


def drop_self(entries: list[Login], names: set[str], user: str) -> list[Login]:
    """Entries minus those naming this machine: the list is shared between
    hosts, so each one's own name is in it, and `localhost` already stands
    for it."""
    return [entry for entry in entries if not is_self(entry, names, user)]


def local_login(entries: list[Login], names: set[str], user: str) -> Login:
    """`localhost` as the list describes this machine: the attributes and
    operations of the first entry naming it that has no via (one with a via
    is a place inside it, not the machine). A bare `localhost` without one."""
    for entry in entries:
        if entry.via is None and is_self(entry, names, user):
            return Login("localhost", attributes=dict(entry.attributes),
                         operations=dict(entry.operations), file=entry.file)
    return Login("localhost")


def ssh_config(entry: str) -> dict[str, str]:
    """What `ssh -G` resolves for entry after ~/.ssh/config: keyword -> value,
    keywords lowercased. Empty if ssh is missing or refuses the name."""
    proc = subprocess.run(["ssh", "-G", entry], capture_output=True, text=True)
    if proc.returncode != 0:
        return {}
    out = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition(" ")
        out.setdefault(key.lower(), value)
    return out


def host_known(entry: str) -> bool:
    """Whether the key of the host entry resolves to is in a known_hosts file
    ssh would consult (user and global ones, hashed entries included)."""
    cfg = ssh_config(entry)
    host = cfg.get("hostname") or entry.rpartition("@")[2]
    port = cfg.get("port", "22")
    name = host if port == "22" else f"[{host}]:{port}"
    files = (cfg.get("userknownhostsfile", "~/.ssh/known_hosts") + " "
             + cfg.get("globalknownhostsfile", "")).split()
    for f in files:
        f = os.path.expanduser(f)
        if not os.path.exists(f):
            continue
        if subprocess.run(["ssh-keygen", "-F", name, "-f", f],
                          capture_output=True).returncode == 0:
            return True
    return False


def confirm_new_hosts(entries: list[str], prog: str) -> None:
    """ssh once, in the foreground, to each login whose host key is not known yet,
    so the yes/no question is asked here. Asked from inside a synchronized
    xpanes window the answer lands in every pane, and under parallel the
    prompt cannot be answered at all: ssh runs in a background process
    group there, and reading /dev/tty stops it. Raises ToolError when a
    host is refused or unreachable."""
    for entry in entries:
        if entry == "localhost" or host_known(entry):
            continue
        print(f"{prog}: {entry}: host key not known yet, connecting once to confirm it",
              file=sys.stderr)
        if subprocess.run(["ssh", entry, "true"]).returncode != 0:
            raise ToolError(f"{entry}: host key not confirmed (answer yes, or drop it from the list)")


def logins(opts: LoginOpts, env=None, found: lists.Lists | None = None) -> list[Login]:
    """The resolved, filtered, optionally picked list; from the lists files,
    or from `found` when the caller has read them already. Raises ToolError
    or pick.Abort."""
    if found is None:
        found = lists.load_all(opts.files, env)
    names, user = self_names(), getpass.getuser()
    entries = [e for e in drop_self(found.logins, names, user)
               if attrs.holds_all(opts.attrs, e.attributes)]
    if not opts.no_local:
        local = local_login(found.logins, names, user)
        if attrs.holds_all(opts.attrs, local.attributes):
            entries.append(local)
    if not entries:
        why = (f"match -a {' '.join(map(str, opts.attrs))}" if opts.attrs
               else "(entries naming this machine are dropped)")
        raise ToolError(f"no logins in {', '.join(found.files)} {why}")
    if opts.pick:
        # Shown with their commands, since one login can be listed twice for
        # two different ones; the picker keeps such twins apart by number.
        entries = pick.pick_from(entries, "logins", label)
    return entries


def label(entry: Login) -> str:
    return entry.login + ("  " + "; ".join(entry.commands) if entry.commands else "")


@click.command(cls=Command, help=__doc__)
@login_options
def cli(opts: LoginOpts) -> int:
    try:
        entries = logins(opts)
    except ToolError as e:
        print(f"ssh-logins: {e}", file=sys.stderr)
        return EXIT_ERROR
    except pick.Abort:
        return pick.EXIT_ABORT
    print("\n".join(e.login for e in entries))
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(cli, argv, "ssh-logins")


if __name__ == "__main__":
    sys.exit(main())
