"""pssh -- run one command on every listed login at once, output tagged by login.

    pssh [-h] [-n] [-N|--no-local] [-p|--pick] [-f FILE] [-i]
         [-P|--paths] [-c|--clone] [-C|--pick-paths] [--] COMMAND [ARG...]

The batch counterpart of xssh: the same login list and -N, -p, -f (see
`ssh-logins -h`), but the command runs non-interactively on each login through
GNU parallel, all at once, every output line prefixed with its login;
`localhost` runs it here. The exit status is the number of logins on which
the command failed. -n prints what would run, and where, instead.

A single COMMAND word is a shell command line, run as is:
`pssh 'cd ~/src && git pull'`. Several words are one command's arguments,
quoted for you, so `pssh ls 'my dir'` lists that one directory.

-P/--paths runs the command once per listed path on each login, from inside
it, and reports each path before its output; a path a host does not have
is reported and skipped, and one where the command fails marks that login
failed. The paths are the `paths` list of the same ssh-lists file the logins
come from (TOML, YAML or JSON; -f names it, see `ssh-logins -h`):

    [[paths]]
    path = "src/webapp"                     # relative to ~ unless absolute
    git_url = "git@github.com:me/webapp.git"    # optional: what to clone it from
    git_branch = "main"                     # optional: -b for the clone

-c/--clone clones a missing path from its git_url before running the
command there (one without a git_url is still skipped), so a fresh host
gets its checkouts on the first run. -C/--pick-paths chooses paths the way
-p chooses logins (fzf, else a numbered menu; see `pick-lines -h`), paths
first when both are given. So `pssh -P git status -s` shows every checkout
on every login, and `pssh -P -c 'git pull --rebase --autostash'` brings them
all up to date.

A login's `commands` in the lists file are for the shells xssh opens; pssh
runs its command as given, on every login alike, and a login listed twice
for two sets of commands is run once.

A login whose host key is not in known_hosts yet is contacted once beforehand,
in the foreground, so ssh's yes/no question can be answered; parallel
would otherwise leave that ssh stopped, waiting on a terminal it cannot
read. Answering no, or an unreachable host, aborts the run.

The remote shell reads no rc file, so aliases and shell functions are not
there and `cd` does not carry over between calls -- put the cd in the
command. -i runs the command inside `zsh -ic` on each login instead, which
brings aliases and functions back at the cost of a full shell start-up per
login. Every run, local included, starts in the home directory, as an ssh
login does. Flags come first: the first word that is not an option starts
the command, so `pssh uptime -p` passes -p to uptime; `--` does the same
explicitly.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys

import click

from . import lists, logins, pick
from .lists import PathInfo


def command_line(words: list[str]) -> str:
    """One word is a shell line as typed; several are one argv, quoted."""
    return words[0] if len(words) == 1 else shlex.join(words)


def paths_list(file: str | None, pick_paths: bool, env=None) -> list[PathInfo]:
    """The paths list of the lists file, optionally picked from. Raises
    ToolError when the file has none, or pick.Abort."""
    path = lists.find_file(file, env)
    found = lists.load(path).paths
    if not found:
        raise logins.ToolError(f"no paths in {path}")
    if pick_paths:
        found = pick.pick_from(found, "paths", lambda p: p.path)
    return found


def paths_script(path_list: list[PathInfo], cmd: str, clone: bool = False) -> str:
    """cmd in each path in turn, cloning missing ones first when asked.
    POSIX sh on purpose: parallel runs it through whichever shell the host
    has."""
    calls = []
    for p in path_list:
        words = [p.path]
        if clone and p.git_url:
            words.append(p.git_url)
            if p.git_branch:
                words.append(p.git_branch)
        calls.append("run " + " ".join(shlex.quote(w) for w in words))
    return f"""rc=0
run() {{ # run DIR [URL [BRANCH]]
    d=$1
    case $d in /*) ;; *) d=$HOME/$d ;; esac
    if [ ! -d "$d" ] && [ -n "$2" ]; then
        echo "== $d: cloning $2"
        git clone -q ${{3:+-b "$3"}} -- "$2" "$d" || {{ echo "== $d: clone FAILED"; rc=1; return; }}
    fi
    if [ ! -d "$d" ]; then echo "== $d: missing, skipped"; return; fi
    echo "== $d"
    ( cd "$d" && {{ {cmd}
    }} ) || {{ echo "== $d: FAILED"; rc=1; }}
}}
{chr(10).join(calls)}
exit $rc"""


def shell_command(cmd: str, interactive: bool = False) -> str:
    """The string parallel hands each host's shell.

    From ~ everywhere: an ssh login lands there, but parallel runs the local
    `:` job in the current directory, which would make `ls` mean two things.
    """
    cmd = "cd ~\n" + cmd
    if interactive:
        cmd = "zsh -ic " + shlex.quote(cmd)
    return cmd


def parallel_argv(entries: list[str], cmd: str) -> list[str]:
    logins = [":" if e == "localhost" else e for e in entries]  # `:` is parallel's "here"
    return ["parallel", "--nonall", "--tag", "--linebuffer", "-S", ",".join(logins), cmd]


@click.command(cls=logins.Command, help=__doc__,
               context_settings={"allow_interspersed_args": False})
@click.option("-n", "--dry-run", is_flag=True, help="print the command and the hosts, run nothing")
@logins.login_options
@click.option("-i", "--interactive", is_flag=True, help="run through `zsh -ic'")
@click.option("-P", "--paths", is_flag=True, help="in every listed path on each login")
@click.option("-c", "--clone", is_flag=True, help="clone a missing path first (implies -P)")
@click.option("-C", "--pick-paths", is_flag=True, help="choose the paths (implies -P)")
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
def cli(opts: logins.LoginOpts, dry_run: bool, interactive: bool, paths: bool, clone: bool,
        pick_paths: bool, command: tuple[str, ...]) -> int:
    """Flags first; the first word that is not one starts the command."""
    paths = paths or clone or pick_paths
    try:
        if not dry_run and not shutil.which("parallel"):
            raise logins.ToolError(
                "GNU parallel not installed (brew install parallel / pacman -S parallel)")
        cmd = command_line(list(command))
        if paths:
            cmd = paths_script(paths_list(opts.file, pick_paths), cmd, clone)
        cmd = shell_command(cmd, interactive)
        # Once per login: a twin listed for other xssh commands is the same machine here.
        entries = list(dict.fromkeys(e.login for e in logins.logins(opts)))
    except logins.ToolError as e:
        print(f"pssh: {e}", file=sys.stderr)
        return logins.EXIT_ERROR
    except pick.Abort:
        return pick.EXIT_ABORT
    if dry_run:
        print(cmd)
        print("-- on:")
        for entry in entries:
            print(f"   {entry}")
        return 0
    try:
        logins.confirm_new_hosts(entries, "pssh")
    except logins.ToolError as e:
        print(f"pssh: {e}", file=sys.stderr)
        return logins.EXIT_ERROR
    os.execvp("parallel", parallel_argv(entries, cmd))
    return 0


def main(argv: list[str] | None = None) -> int:
    return logins.run(cli, argv, "pssh")


if __name__ == "__main__":
    sys.exit(main())
