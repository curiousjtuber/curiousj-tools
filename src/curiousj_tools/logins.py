"""ssh-logins -- print the login list xssh and pssh work from, optionally picking a subset.

    ssh-logins [-h] [-N|--no-local] [-p|--pick] [-f FILE]

One entry per line: every login ([user@]host) in the lists file, then
`localhost` last, which consumers turn into whatever "this machine" means
for them (xssh: a local pane, pssh: a local run); -N/--no-local leaves it
out. Entries that name this machine, for this user, are dropped, so one
list can serve every host on it. -p/--pick shows the list in fzf (TAB marks
several) and prints only the marked entries; without fzf, a numbered menu
(see `pick-lines -h`). Aborting the picker exits 130 with nothing printed.

The logins are the `logins` list of the ssh-lists file, TOML, YAML or JSON
(see the README, or the curiousj_tools.lists docstring, for the shape).
Without -f the first readable of these is used:

    $SSH_LISTS_FILE                 one file, wherever it is
    DIR/ssh-lists.toml|yaml|yml|json    for each DIR in $SSH_LISTS_PATH
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

from . import lists, pick
from .lists import Login, ToolError  # noqa: F401  (re-exported)

EXIT_ERROR = 1
EXIT_USAGE = 2


@dataclass
class LoginOpts:
    """The login-selection flags xssh and pssh share with ssh-logins."""

    no_local: bool = False
    pick: bool = False
    file: str | None = None


def login_options(f):
    """The -N, -p and -f every login tool takes; the callback gets them as
    one LoginOpts named opts."""
    @click.option("-f", "file", metavar="FILE", help="the ssh-lists file to read")
    @click.option("-p", "--pick", is_flag=True, help="choose logins interactively")
    @click.option("-N", "--no-local", is_flag=True, help="leave localhost out")
    @functools.wraps(f)
    def wrapper(no_local, pick, file, **kw):
        return f(LoginOpts(no_local, pick, file), **kw)
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


def drop_self(entries: list[Login], names: set[str], user: str) -> list[Login]:
    """Entries minus those naming this machine: the list is shared between
    hosts, so each one's own name is in it, and `localhost` already stands
    for it. An entry for another user on this machine is kept: that is a
    different login, not this one."""
    kept = []
    for entry in entries:
        entry_user, _, host = entry.login.rpartition("@")
        host = host.lower()
        mine = host in names or host.split(".", 1)[0] in names
        if mine and (not entry_user or entry_user == user):
            continue
        kept.append(entry)
    return kept


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


def logins(opts: LoginOpts, env=None) -> list[Login]:
    """The resolved, filtered, optionally picked list. Raises ToolError or
    pick.Abort."""
    path = lists.find_file(opts.file, env)
    entries = drop_self(lists.load(path).logins, self_names(), getpass.getuser())
    if not entries and opts.no_local:
        raise ToolError(f"no logins in {path} (entries naming this machine are dropped)")
    if not opts.no_local:
        entries.append(Login("localhost"))
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
