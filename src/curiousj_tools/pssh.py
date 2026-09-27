"""pssh -- run one command, or the lists' operations, on every listed login at once, output tagged by login.

    pssh [-h] [-n] [-s] [-N|--no-local] [-a TERM]... [-p|--pick] [-f FILE]... [-i]
         [-P|--paths] [-A TERM]... [-c|--clone] [-C|--pick-paths] [--] COMMAND [ARG...]
    pssh [same flags] -o NAME...
    pssh [-f FILE]... -L|--list-ops

The batch counterpart of xssh: the same login list and -N, -a, -p, -f (see
`ssh-logins -h`), but the command runs non-interactively on each login through
GNU parallel, all at once, every output line prefixed with its login;
'localhost' runs it here. The exit status is the number of logins on which
the command failed. -n prints what would run, and where, instead.

A single COMMAND word is a shell command line, run as is:
`pssh 'cd ~/src && git pull'`. Several words are one command's arguments,
quoted for you, so `pssh ls 'my dir'` lists that one directory.

-P/--paths runs the command once per listed path on each login, from inside
it, and reports each path before its output; a path a host does not have
is reported and skipped, and one where the command fails marks that login
failed. The paths are the 'paths' list of the same ssh-lists files the logins
come from (TOML, YAML or JSON; -f names them, see `ssh-logins -h`):

    [[paths]]
    path = "src/webapp"                     # relative to ~ unless absolute
    git_url = "git@github.com:me/webapp.git"    # optional: what to clone it from
    git_branch = "main"                     # optional: -b for the clone
    attributes = ["git", "py-project"]      # optional: what -A and operations select on

-c/--clone clones a missing path from its git_url before running the
command there (one without a git_url is still skipped), so a fresh host
gets its checkouts on the first run. -C/--pick-paths chooses paths the way
-p chooses logins (fzf, else a numbered menu; see `pick-lines -h`), paths
first when both are given; -A/--path-attr TERM keeps the paths whose
attributes satisfy TERM, as -a does for logins. So `pssh -P git status -s`
shows every checkout on every login, and `pssh -A git -c 'git pull'` brings
the git ones up to date.

-o/--op NAME runs an operation instead of a COMMAND, one that says itself
what it runs and where. git-pull, uv-tool-update, mise-update, cachy-update,
brew-upgrade, system-update and update-all come built in; the lists files
add their own, and replace a built-in by defining its name:

    [operations.git-pull]
    command = "git pull --rebase --autostash"
    paths = "git"                           # per path, in the paths matching (true: all)
    clone = true                            # as -c

    [operations.mise-update]
    command = "mise self-update -y && mise upgrade"
    logins = "mise"                         # per login, on the logins matching (absent: all)

    [operations.cachy-update]
    command = "cachy-update"
    logins = "cachyos"
    serial = true                           # asks questions: as -s

    [operations.system-update]
    operations = ["cachy-update", "brew-upgrade"]     # a group: each login runs what it matches

A condition is a term ('mise', 'arch=x86_64', '!mise', 'arch!=x86_64'), a
list of terms that all hold, or a table with 'all', 'any', 'none'. A login
or path can carry its own command for an operation, `operations = {
git-pull = "git pull --ff-only" }`, and then takes part with it whatever the
condition says; the name need not be in the [operations.*] table at all.
Given several -o, or a group, each login runs the operations that apply to
it in order, in one shell, each announced by "== NAME" and, per path, by
"== DIR" as -P does; a login none applies to is left alone. -A narrows the
paths, -C picks them, -c clones for every per-path operation, -a narrows
the logins: `pssh -o git-pull -A py-project`, `pssh -a cachyos -o
system-update`. -L/--list-ops lists the operations, the built-in ones first,
where each runs and what, the entries' own commands under it.

-s/--serial runs one login at a time, in the foreground, through `ssh -t`
('localhost': a local shell), each announced by "== LOGIN", so a command
that asks questions -- a package manager, sudo -- can be answered; the
output is not tagged. An operation with "serial = true" makes the run
serial by itself. (xssh -o runs an operation in synchronized panes instead,
one keystroke answering every login.)

A login's 'commands' in the lists file are for the shells xssh opens; pssh
runs its command as given, on every login alike. A login's 'via' is pssh's:
a command line the command is run through there, handed `sh -c `...'` (or
`zsh -ic `...'` with -i), for a place the login shell alone does not reach:

    [[logins]]
    login = "alice@devbox"
    commands = ["distrobox enter dev -nw"]      # xssh: the pane lands in the container
    via = "distrobox enter dev -nw --"          # pssh: runs its command in there too

Inside, '~' is that place's home, so with the same login listed plainly as
well, `pssh -P -c ...` keeps a container's separate home current alongside
the host's. Entries that repeat a login with the same via are run once. Run
on the host itself, both entries give way to 'localhost', which has no via,
so the container is reached only from the other hosts.

A login whose host key is not in known_hosts yet is contacted once beforehand,
in the foreground, so ssh's yes/no question can be answered; parallel
would otherwise leave that ssh stopped, waiting on a terminal it cannot
read. Answering no, or an unreachable host, aborts the run.

The remote shell reads no rc file, so aliases and shell functions are not
there and 'cd' does not carry over between calls -- put the cd in the
command. -i runs the command inside `zsh -ic` on each login instead, which
brings aliases and functions back at the cost of a full shell start-up per
login. Every run, local included, starts in the home directory, as an ssh
login does. Flags come first: the first word that is not an option starts
the command, so `pssh uptime -p` passes -p to uptime; '--' does the same
explicitly.
"""

from __future__ import annotations

import functools
import os
import shlex
import shutil
import subprocess
import sys

import click

from . import attrs, cmdline, lists, logins, pick
from .lists import Login, Operation, PathInfo, ToolError


def command_line(words: list[str]) -> str:
    """One word is a shell line as typed; several are one argv, quoted."""
    return words[0] if len(words) == 1 else shlex.join(words)


def paths_list(found: lists.Lists, pick_paths: bool = False,
               terms: tuple[attrs.Term, ...] = ()) -> list[PathInfo]:
    """The paths of the lists, those -A keeps, optionally picked from.
    Raises ToolError when none is left, or pick.Abort."""
    path_list = [p for p in found.paths if attrs.holds_all(terms, p.attributes)]
    if not path_list:
        why = f" match -A {' '.join(map(str, terms))}" if terms and found.paths else ""
        raise ToolError(f"no paths in {', '.join(found.files)}{why}")
    if pick_paths:
        path_list = pick.pick_from(path_list, "paths", lambda p: p.path)
    return path_list


# The per-login script. POSIX sh on purpose: parallel and ssh run it through
# whichever shell the host has. The command comes in $cmd, set before the
# calls that use it, so one function serves every operation.
RUN = '''run() { # run DIR [URL [BRANCH]]: $cmd in DIR, cloned from URL first when missing
    d=$1
    case $d in /*) ;; *) d=$HOME/$d ;; esac
    if [ ! -d "$d" ] && [ -n "$2" ]; then
        echo "== $d: cloning $2"
        git clone -q ${3:+-b "$3"} -- "$2" "$d" || { echo "== $d: clone FAILED"; rc=1; return; }
    fi
    if [ ! -d "$d" ]; then echo "== $d: missing, skipped"; return; fi
    echo "== $d"
    ( cd "$d" && eval "$cmd" ) || { echo "== $d: FAILED"; rc=1; }
}'''


def set_cmd(cmd: str) -> str:
    return "cmd=" + shlex.quote(cmd)


def path_calls(pairs: list[tuple[PathInfo, str]], clone: bool = False) -> list[str]:
    """A 'run' per path with its command, set whenever it changes; the url
    and branch along when a missing path is to be cloned."""
    lines: list[str] = []
    current = None
    for p, cmd in pairs:
        if cmd != current:
            lines.append(set_cmd(cmd))
            current = cmd
        words = [p.path]
        if clone and p.git_url:
            words.append(p.git_url)
            if p.git_branch:
                words.append(p.git_branch)
        lines.append("run " + " ".join(shlex.quote(w) for w in words))
    return lines


def home_call(name: str, cmd: str) -> list[str]:
    """cmd from the home directory, a failure reported under name."""
    failed = shlex.quote(f"== {name}: FAILED")
    return [set_cmd(cmd), f'( eval "$cmd" ) || {{ echo {failed}; rc=1; }}']


def header(name: str) -> str:
    return "echo " + shlex.quote(f"== {name}")


def script(lines: list[str]) -> str:
    return "rc=0\n" + RUN + "\n" + "\n".join(lines) + "\nexit $rc"


def paths_script(path_list: list[PathInfo], cmd: str, clone: bool = False) -> str:
    """cmd in each path in turn, cloning missing ones first when asked."""
    return script(path_calls([(p, cmd) for p in path_list], clone))


def expand(names: list[str], ops: dict[str, Operation]) -> list[Operation]:
    """The operations named, groups opened depth-first, each once, in order.
    Raises ToolError on a name the lists do not have."""
    out: dict[str, Operation] = {}

    def walk(name: str, inside: str | None) -> None:
        op = ops.get(name)
        if op is None:
            where = f" (in {inside})" if inside else ""
            known = ", ".join(sorted(ops)) or "none"
            raise ToolError(f"unknown operation {name!r}{where}; known: {known}")
        if op.group:
            for member in op.members:
                walk(member, name)
        else:
            out.setdefault(name, op)

    for name in names:
        walk(name, None)
    return list(out.values())


def describe(op: Operation, found: lists.Lists) -> tuple[str, str, list[str]]:
    """An operation as -L shows it: where it runs and its flags, its command
    or members, and a line, indented, per entry with its own command for it.
    Columns stay apart as values: a command may hold any character."""
    if op.group:
        where, what = "group", ", ".join(op.members)
    else:
        if op.per_path:
            where = "per path" + ("" if op.paths == attrs.EVERYTHING else f" {op.paths}")
            if op.logins != attrs.EVERYTHING:
                where += f" on logins {op.logins}"
        else:
            where = "per login" + ("" if op.logins == attrs.EVERYTHING else f" {op.logins}")
        where += "".join(f", {flag}" for flag in ("clone", "serial") if getattr(op, flag))
        what = op.command or "(the entries' own commands)"
    entries = found.paths if op.per_path else found.logins
    own = [f"    {getattr(entry, 'path' if op.per_path else 'login')}: {entry.operations[op.name]}"
           for entry in entries if op.name in entry.operations]
    return where, what, own


def list_operations(found: lists.Lists) -> str:
    """The operations of the lists, one per line, name, place and command
    in columns, for -L."""
    if not found.operations:
        return f"no operations in {', '.join(found.files)}"
    rows = [(name, *describe(op, found)) for name, op in found.operations.items()]
    width = max(len(name) for name, *_ in rows)
    place = max(len(where) for _, where, _, _ in rows)
    out = []
    for name, where, what, own in rows:
        out.append(f"{name.ljust(width)}  {where.ljust(place)}  {what}")
        out.extend(own)
    return "\n".join(out)


def per_login_command(op: Operation, entry: Login) -> str | None:
    """What a per-login operation runs on a login: its own command, else the
    operation's when the login matches; None when nothing."""
    own = entry.operations.get(op.name)
    if own is not None:
        return own
    if op.command and op.logins.matches(entry.attributes):
        return op.command
    return None


def per_path_command(op: Operation, p: PathInfo) -> str | None:
    own = p.operations.get(op.name)
    if own is not None:
        return own
    if op.command and op.paths is not None and op.paths.matches(p.attributes):
        return op.command
    return None


def login_script(entry: Login, ops: list[Operation], path_list: list[PathInfo],
                 clone: bool = False) -> str | None:
    """The script running the operations that apply to the login, in order,
    or None when none does."""
    lines: list[str] = []
    for op in ops:
        if op.per_path:
            if not op.logins.matches(entry.attributes):
                continue
            pairs = [(p, cmd) for p in path_list if (cmd := per_path_command(op, p))]
            if pairs:
                lines.append(header(op.name))
                lines.extend(path_calls(pairs, clone or op.clone))
        else:
            cmd = per_login_command(op, entry)
            if cmd is not None:
                lines.append(header(op.name))
                lines.extend(home_call(op.name, cmd))
    return script(lines) if lines else None


def shell_command(cmd: str, interactive: bool = False) -> str:
    """The string parallel hands each host's shell.

    From ~ everywhere: an ssh login lands there, but parallel runs the local
    ':' job in the current directory, which would make 'ls' mean two things.
    """
    cmd = "cd ~\n" + cmd
    if interactive:
        cmd = "zsh -ic " + shlex.quote(cmd)
    return cmd


def wrap(cmd: str, via: str | None, interactive: bool = False) -> str:
    """The string parallel hands a login's shell: shell_command, run through
    via when the login has one -- as `zsh -ic `...'` under -i, which is
    already a command line, else as `sh -c `...'`."""
    cmd = shell_command(cmd, interactive)
    if not via:
        return cmd
    return via + " " + (cmd if interactive else "sh -c " + shlex.quote(cmd))


def runs(pairs: list[tuple[Login, str]], interactive: bool = False) -> dict[str, list[str]]:
    """What runs where: each distinct final command with its logins, in list
    order, from (login, its command) pairs. A login listed twice with the
    same via is one run; one listed plainly and with a via is two, one for
    each place."""
    out: dict[str, list[str]] = {}
    for entry, cmd in pairs:
        line = wrap(cmd, entry.via, interactive)
        group = out.setdefault(line, [])
        if entry.login not in group:
            group.append(entry.login)
    return out


def parallel_argv(entries: list[str], cmd: str) -> list[str]:
    hosts = [":" if e == "localhost" else e for e in entries]  # ':' is parallel's "here"
    return ["parallel", "--nonall", "--tag", "--linebuffer", "-S", ",".join(hosts), cmd]


def serial_argv(login: str, cmd: str) -> list[str]:
    return ["sh", "-c", cmd] if login == "localhost" else ["ssh", "-t", login, cmd]


def serial_run(groups: dict[str, list[str]]) -> int:
    """Each login in turn, in the foreground: the number that failed; 130
    when interrupted."""
    failed = 0
    try:
        for line, entries in groups.items():
            for login in entries:
                print(f"== {login}", flush=True)
                if subprocess.run(serial_argv(login, line)).returncode != 0:
                    failed += 1
    except KeyboardInterrupt:
        return pick.EXIT_ABORT
    return failed


def path_options(f=None, *, short: bool = True, implies: str = " (implies -P)"):
    """The -A, -c and -C of a tool that works in the listed paths; long
    names only for -c and -C where those letters are taken (xssh hands
    them to xpanes)."""
    if f is None:
        return functools.partial(path_options, short=short, implies=implies)
    pick_names = ("-C", "--pick-paths") if short else ("--pick-paths",)
    clone_names = ("-c", "--clone") if short else ("--clone",)

    @click.option(*pick_names, "pick_paths", is_flag=True, help="choose the paths" + implies)
    @click.option(*clone_names, "clone", is_flag=True, help="clone a missing path first" + implies)
    @click.option("-A", "--path-attr", "path_attrs", metavar="TERM", multiple=True,
                  callback=logins.attr_terms,
                  help="keep paths with the attribute, as -a for logins" + implies)
    @functools.wraps(f)
    def wrapper(*args, **kw):
        return f(*args, **kw)
    return wrapper


def need_parallel() -> None:
    if not shutil.which("parallel"):
        raise ToolError("GNU parallel not installed (brew install parallel / pacman -S parallel)")


@click.command(cls=cmdline.Command, help=__doc__,
               context_settings={"allow_interspersed_args": False})
@click.option("-n", "--dry-run", is_flag=True, help="print the command and the hosts, run nothing")
@click.option("-s", "--serial", is_flag=True,
              help="one login at a time in the foreground, through `ssh -t`")
@logins.login_options
@click.option("-i", "--interactive", is_flag=True, help="run through `zsh -ic`")
@click.option("-o", "--op", "ops", metavar="NAME", multiple=True,
              help="run the operation NAME instead of a COMMAND (repeatable)")
@click.option("-L", "--list-ops", is_flag=True, help="list the operations, built-in and the lists files'")
@click.option("-P", "--paths", is_flag=True, help="in every listed path on each login")
@path_options
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def cli(opts: logins.LoginOpts, dry_run: bool, serial: bool, interactive: bool,
        ops: tuple[str, ...], list_ops: bool, paths: bool, path_attrs: tuple[attrs.Term, ...],
        clone: bool, pick_paths: bool, command: tuple[str, ...]) -> int:
    """Flags first; the first word that is not one starts the command."""
    ctx = click.get_current_context()
    if list_ops:
        try:
            print(list_operations(lists.load_all(opts.files)))
        except ToolError as e:
            print(f"pssh: {e}", file=sys.stderr)
            return cmdline.EXIT_ERROR
        return 0
    if not command and not ops:
        raise click.UsageError("Missing argument 'COMMAND...' (or -o NAME).", ctx)
    if command and ops:
        raise click.UsageError("COMMAND and -o NAME are alternatives.", ctx)
    if paths and ops:
        raise click.UsageError("-P and -o: an operation says itself where it runs.", ctx)
    paths = paths or clone or pick_paths or bool(path_attrs)
    skipped: list[str] = []
    try:
        found = lists.load_all(opts.files) if paths or ops else None
        leaves = expand(list(ops), found.operations) if ops else []
        askers = [op.name for op in leaves if op.serial]
        if askers and not serial:
            serial = True
            print(f"pssh: {', '.join(askers)} asks questions: one login at a time",
                  file=sys.stderr)
        # Once serial is settled, and before any picker: a run parallel is
        # missing for asks nothing first.
        if not dry_run and not serial:
            need_parallel()
        if ops:
            path_list = (paths_list(found, pick_paths, path_attrs)
                         if any(op.per_path for op in leaves) else [])
            entries = logins.select(opts, found=found)
            scripts = [(e, login_script(e, leaves, path_list, clone)) for e in entries]
            skipped = list(dict.fromkeys(e.login for e, s in scripts if s is None))
            groups = runs([(e, s) for e, s in scripts if s], interactive)
        else:
            cmd = command_line(list(command))
            if paths:
                cmd = paths_script(paths_list(found, pick_paths, path_attrs), cmd, clone)
            groups = runs([(e, cmd) for e in logins.select(opts, found=found)], interactive)
    except ToolError as e:
        print(f"pssh: {e}", file=sys.stderr)
        return cmdline.EXIT_ERROR
    except pick.Abort:
        return pick.EXIT_ABORT
    if dry_run:
        for line, entries in groups.items():
            print(line)
            print("-- one at a time on:" if serial else "-- on:")
            for entry in entries:
                print(f"   {entry}")
        if skipped:
            print("-- not contacted (no operation applies):")
            for login in skipped:
                print(f"   {login}")
        return 0
    for login in skipped:
        print(f"pssh: {login}: no operation applies, skipped", file=sys.stderr)
    if not groups:
        print("pssh: no login takes part", file=sys.stderr)
        return 0
    try:
        logins.confirm_new_hosts(list(dict.fromkeys(e for g in groups.values() for e in g)),
                                 "pssh")
    except ToolError as e:
        print(f"pssh: {e}", file=sys.stderr)
        return cmdline.EXIT_ERROR
    if serial:
        return serial_run(groups)
    if len(groups) == 1:
        (line, entries), = groups.items()
        os.execvp("parallel", parallel_argv(entries, line))
        return 0
    # One parallel per distinct command, all at once; each exits with its
    # number of failed logins, so the sum is what one run would have given.
    procs = [subprocess.Popen(parallel_argv(entries, line)) for line, entries in groups.items()]
    return sum(p.wait() for p in procs)


def main(argv: list[str] | None = None) -> int:
    return cmdline.run(cli, argv, "pssh")


if __name__ == "__main__":
    sys.exit(main())
