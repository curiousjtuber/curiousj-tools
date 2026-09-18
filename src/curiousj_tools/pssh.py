"""pssh -- run one command on every listed host at once, output tagged by host.

    pssh [-h] [-n] [-N|--no-local] [-p|--pick] [-f FILE] [-i]
         [-P|--paths] [-c|--clone] [-C|--pick-paths] [--] COMMAND [ARG...]

The batch counterpart of xssh: the same host list and -N, -p, -f (see
`ssh-hosts -h`), but the command runs non-interactively on each host through
GNU parallel, all hosts at once, every output line prefixed with its host;
`localhost` runs it here. The exit status is the number of hosts on which
the command failed. -n prints what would run, and where, instead.

A single COMMAND word is a shell command line, run as is:
`pssh 'cd ~/src && git pull'`. Several words are one command's arguments,
quoted for you, so `pssh ls 'my dir'` lists that one directory.

-P/--paths runs the command once per listed path on each host, from inside
it, and reports each path before its output; a path a host does not have
is reported and skipped, and one where the command fails marks that host
failed. The paths are the `paths` list of the same ssh-lists file the hosts
come from (TOML, YAML or JSON; -f names it, see `ssh-hosts -h`):

    [[paths]]
    path = "src/webapp"                     # relative to ~ unless absolute
    git_url = "git@github.com:me/webapp.git"    # optional: what to clone it from
    git_branch = "main"                     # optional: -b for the clone

-c/--clone clones a missing path from its git_url before running the
command there (one without a git_url is still skipped), so a fresh host
gets its checkouts on the first run. -C/--pick-paths chooses paths the way
-p chooses hosts (fzf, else a numbered menu; see `pick-lines -h`), paths
first when both are given. So `pssh -P git status -s` shows every checkout
on every host, and `pssh -P -c 'git pull --rebase --autostash'` brings them
all up to date.

A host's `commands` in the lists file are for the login shells xssh opens;
pssh runs its command as given, on every host alike.

A host whose key is not in known_hosts yet is contacted once beforehand,
in the foreground, so ssh's yes/no question can be answered; parallel
would otherwise leave that ssh stopped, waiting on a terminal it cannot
read. Answering no, or an unreachable host, aborts the run.

The remote shell reads no rc file, so aliases and shell functions are not
there and `cd` does not carry over between calls -- put the cd in the
command. -i runs the command inside `zsh -ic` on each host instead, which
brings aliases and functions back at the cost of a full shell start-up per
host. Every run, local included, starts in the home directory, as an ssh
login does. Flags come first: the first word that is not an option starts
the command, so `pssh uptime -p` passes -p to uptime; `--` does the same
explicitly.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from dataclasses import dataclass

from . import hosts, lists, pick
from .lists import PathInfo

USAGE = ("usage: pssh [-n] [-N] [-p] [-f FILE] [-i] [-P] [-c] [-C] "
         "[--] COMMAND [ARG...]")


@dataclass
class Opts:
    hosts: hosts.HostOpts
    interactive: bool = False
    dry_run: bool = False
    paths: bool = False
    clone: bool = False
    pick_paths: bool = False


def split_args(argv: list[str]) -> tuple[Opts, list[str]]:
    """Leading flags, then the command. Raises UsageError on a bad flag."""
    opts = Opts(hosts.HostOpts())
    i = 0
    while i < len(argv):
        j = hosts.take_host_opt(argv, i, opts.hosts, "pssh")
        if j != i:
            i = j
            continue
        arg = argv[i]
        if arg in ("-i", "--interactive"):
            opts.interactive = True
        elif arg in ("-n", "--dry-run"):
            opts.dry_run = True
        elif arg in ("-P", "--paths"):
            opts.paths = True
        elif arg in ("-c", "--clone"):
            opts.paths = True
            opts.clone = True
        elif arg in ("-C", "--pick-paths"):
            opts.paths = True
            opts.pick_paths = True
        elif arg == "--":
            i += 1
            break
        elif arg.startswith("-"):
            raise hosts.UsageError(f"unknown option {arg}")
        else:
            break
        i += 1
    return opts, argv[i:]


def command_line(words: list[str]) -> str:
    """One word is a shell line as typed; several are one argv, quoted."""
    return words[0] if len(words) == 1 else shlex.join(words)


def paths(file: str | None, pick_paths: bool, env=None) -> list[PathInfo]:
    """The paths list of the lists file, optionally picked from. Raises
    HostsError when the file has none, or pick.Abort."""
    path = lists.find_file(file, env)
    found = lists.load(path).paths
    if not found:
        raise hosts.HostsError(f"no paths in {path}")
    if pick_paths:
        chosen = set(pick.pick([p.path for p in found], "paths"))
        found = [p for p in found if p.path in chosen]
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


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.rstrip())
        return 0
    try:
        opts, words = split_args(argv)
        if not words:
            print(USAGE, file=sys.stderr)
            return hosts.EXIT_USAGE
        if not opts.dry_run and not shutil.which("parallel"):
            raise hosts.HostsError(
                "GNU parallel not installed (brew install parallel / pacman -S parallel)")
        cmd = command_line(words)
        if opts.paths:
            cmd = paths_script(paths(opts.hosts.file, opts.pick_paths), cmd, opts.clone)
        cmd = shell_command(cmd, opts.interactive)
        entries = [e.host for e in hosts.hosts(opts.hosts)]
    except hosts.UsageError as e:
        print(f"pssh: {e}", file=sys.stderr)
        return hosts.EXIT_USAGE
    except hosts.HostsError as e:
        print(f"pssh: {e}", file=sys.stderr)
        return hosts.EXIT_ERROR
    except pick.Abort:
        return pick.EXIT_ABORT
    if opts.dry_run:
        print(cmd)
        print("-- on:")
        for entry in entries:
            print(f"   {entry}")
        return 0
    try:
        hosts.confirm_new_hosts(entries, "pssh")
    except hosts.HostsError as e:
        print(f"pssh: {e}", file=sys.stderr)
        return hosts.EXIT_ERROR
    os.execvp("parallel", parallel_argv(entries, cmd))


if __name__ == "__main__":
    sys.exit(main())
