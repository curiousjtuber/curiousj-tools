"""ssh-logins -- print the login list xssh and pssh work from, optionally picking a subset.

    ssh-logins [-h] [-N|--no-local] [-a TERM]... [-p|--pick] [-f FILE]...

One entry per line: every login ([user@]host) in the lists files, then
'localhost' last, which consumers turn into whatever "this machine" means
for them (xssh: a local pane, pssh: a local run); -N/--no-local leaves it
out. Entries that name this machine, for this user, become 'localhost',
so one list can serve every host on it: each keeps its commands, via,
attributes and operations (xssh runs the commands in a local pane, pssh
runs through the via here), so a machine listed plainly and with a via is
'localhost' twice. A plain 'localhost' comes first when every such entry
has a via, or there is none. -p/--pick shows
the list in fzf (TAB marks several) and prints only the marked entries;
without fzf, a numbered menu (see `pick-lines -h`).
Aborting the picker exits 130 with nothing printed.

-a/--attr TERM keeps the logins whose attributes satisfy TERM: 'mise' has
it, 'arch=x86_64' has it with that value, '!mise' lacks it, 'arch!=x86_64'
lacks it or has another value; given several times, every TERM has to hold.
A 'localhost' is kept or dropped like any other, by the attributes it took
over; the plain one added for the machine has none. Quote a '!' for the
shell.

The logins are the 'logins' list of the ssh-lists files, TOML, YAML or JSON
(see the README, or the curiousj_tools.lists docstring, for the shape).
Which files, merged in this order:

    -f FILE                         on the command line, repeatable
    $SSH_LISTS_FILE                 colon-separated files, without -f
    DIR/*.toml|yaml|yml|json        otherwise, for each DIR in $SSH_LISTS_PATH
                                    (colon-separated), default ~/.config/ssh-lists
"""

from __future__ import annotations

import functools
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass

import click

from . import attrs, lists, pick, sshutil
from .cmdline import EXIT_ERROR, Command, run
from .lists import Login, ToolError

@dataclass
class LoginOpts:
    """The login-selection flags xssh and pssh share with ssh-logins."""

    no_local: bool = False
    choose: bool = False                  # -p: pick some interactively
    files: tuple[str, ...] = ()
    terms: tuple[attrs.Term, ...] = ()    # -a: keep those whose attributes satisfy all


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
    @click.option("-p", "--pick", "choose", is_flag=True, help="choose logins interactively")
    @click.option("-a", "--attr", "terms", metavar="TERM", multiple=True, callback=attr_terms,
                  help="keep logins with the attribute: key, key=value, !key, key!=value (repeatable)")
    @click.option("-N", "--no-local", is_flag=True, help="leave localhost out")
    @functools.wraps(f)
    def wrapper(no_local, terms, choose, files, **kw):
        return f(LoginOpts(no_local, choose, tuple(files), terms), **kw)
    return wrapper


def self_names() -> set[str]:
    """Lowercased names this machine answers to: hostname, its first label,
    the loopback names and, on macOS, the Bonjour name ('my-mac.local' for a
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
    hosts, so each one's own name is in it, and 'localhost' already stands
    for it."""
    return [entry for entry in entries if not is_self(entry, names, user)]


def local_logins(entries: list[Login], names: set[str], user: str) -> list[Login]:
    """'localhost' as the list describes this machine: one per entry naming
    it, in list order, with that entry's commands, via, attributes and
    operations -- a via entry is a place inside the machine, a container,
    reached here as from any other host. A bare 'localhost' first when no
    entry names the machine itself, without a via."""
    local = [Login("localhost", list(e.commands), e.via, dict(e.attributes),
                   dict(e.operations), e.file)
             for e in entries if is_self(e, names, user)]
    if all(e.via is not None for e in local):
        local.insert(0, Login("localhost"))
    return local


def confirm_new_hosts(entries: list[str], prog: str) -> None:
    """ssh once, in the foreground, to each login whose host key is not known yet,
    so the yes/no question is asked here. Asked from inside a synchronized
    xpanes window the answer lands in every pane, and under parallel the
    prompt cannot be answered at all: ssh runs in a background process
    group there, and reading /dev/tty stops it. Raises ToolError when a
    host is refused or unreachable."""
    for entry in entries:
        if entry == "localhost" or sshutil.host_known(entry):
            continue
        print(f"{prog}: {entry}: host key not known yet, connecting once to confirm it",
              file=sys.stderr)
        if subprocess.run(sshutil.probe_ssh(entry) + ["true"]).returncode != 0:
            raise ToolError(f"{entry}: host key not confirmed (answer yes, or drop it from the list)")


def select(opts: LoginOpts, env=None, found: lists.Lists | None = None) -> list[Login]:
    """The resolved, filtered, optionally picked list; from the lists files,
    or from 'found' when the caller has read them already. Raises ToolError
    or pick.Abort."""
    if found is None:
        found = lists.load_all(opts.files, env)
    names, user = self_names(), sshutil.login_name()
    entries = [e for e in drop_self(found.logins, names, user)
               if attrs.holds_all(opts.terms, e.attributes)]
    if not opts.no_local:
        entries += [local for local in local_logins(found.logins, names, user)
                    if attrs.holds_all(opts.terms, local.attributes)]
    if not entries:
        why = (f"match -a {' '.join(map(str, opts.terms))}" if opts.terms
               else "(entries naming this machine are dropped)")
        raise ToolError(f"no logins in {', '.join(found.files)} {why}")
    if opts.choose:
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
        entries = select(opts)
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
