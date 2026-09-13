"""pssh -- run one command on every listed host at once, output tagged by host.

    pssh [-h] [-n] [-N|--no-local] [-p|--pick] [-f HOSTFILE] [-i]
         [-d|--dirs] [-c|--clone] [-r DIRFILE] [-D|--pick-dirs] [--] COMMAND [ARG...]

The batch counterpart of xssh: the same host list and -N, -p, -f (see
`ssh-hosts -h`), but the command runs non-interactively on each host through
GNU parallel, all hosts at once, every output line prefixed with its host;
`localhost` runs it here. The exit status is the number of hosts on which
the command failed. -n prints what would run, and where, instead.

A single COMMAND word is a shell command line, run as is:
`pssh 'cd ~/src && git pull'`. Several words are one command's arguments,
quoted for you, so `pssh ls 'my dir'` lists that one directory.

-d/--dirs runs the command once per listed directory on each host, from
inside it, and reports each directory before its output; a directory a host
does not have is reported and skipped, and one where the command fails
marks that host failed. The list is a TOML file of [[dir]] tables:

    [[dir]]
    path = "src/webapp"                     # relative to ~ unless absolute
    url = "git@github.com:me/webapp.git"    # optional: what to clone it from
    branch = "main"                         # optional: -b for the clone

-c/--clone clones a missing directory from its url before running the
command there (one without a url is still skipped), so a fresh host gets
its checkouts on the first run. -r names the file, otherwise the first
readable of:

    $SSH_DIRS                       explicit override
    $SSH_LISTS_DIR/ssh-dirs.toml    a directory of lists, e.g. a private repo
    ~/.config/ssh-dirs.toml         ($XDG_CONFIG_HOME/ssh-dirs.toml)

-D/--pick-dirs chooses directories the way -p chooses hosts (fzf, else a
numbered menu; see `pick-lines -h`), directories first when both are given.
So `pssh -d git status -s` shows every checkout on every host, and
`pssh -d -c 'git pull --rebase --autostash'` brings them all up to date.

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
import tomllib
from dataclasses import dataclass

from . import hosts, pick

USAGE = ("usage: pssh [-n] [-N] [-p] [-f HOSTFILE] [-i] [-d] [-c] [-r DIRFILE] [-D] "
         "[--] COMMAND [ARG...]")
DIRS_NAME = "ssh-dirs.toml"


@dataclass
class Opts:
    hosts: hosts.HostOpts
    interactive: bool = False
    dry_run: bool = False
    dirs: bool = False
    clone: bool = False
    dirfile: str | None = None
    pick_dirs: bool = False


@dataclass
class Dir:
    path: str
    url: str | None = None
    branch: str | None = None


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
        elif arg in ("-d", "--dirs"):
            opts.dirs = True
        elif arg in ("-c", "--clone"):
            opts.dirs = True
            opts.clone = True
        elif arg in ("-D", "--pick-dirs"):
            opts.dirs = True
            opts.pick_dirs = True
        elif arg == "-r":
            if i + 1 >= len(argv):
                raise hosts.UsageError("-r needs a file")
            opts.dirs = True
            opts.dirfile = argv[i + 1]
            i += 1
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


def read_dirs(path: str) -> list[Dir]:
    """The [[dir]] tables of a TOML list file. Raises HostsError on a
    malformed file, naming the entry."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise hosts.HostsError(f"{path}: {e}") from None
    tables = data.get("dir", [])
    if not isinstance(tables, list):
        raise hosts.HostsError(f"{path}: 'dir' must be [[dir]] tables")
    found = []
    for n, t in enumerate(tables, 1):
        if not isinstance(t, dict) or not isinstance(t.get("path"), str) or not t["path"]:
            raise hosts.HostsError(f"{path}: [[dir]] entry {n} needs a path")
        for key in ("url", "branch"):
            if key in t and not isinstance(t[key], str):
                raise hosts.HostsError(f"{path}: [[dir]] {t['path']}: {key} must be a string")
        found.append(Dir(t["path"], t.get("url"), t.get("branch")))
    return found


def dirs(path_opt: str | None, pick_dirs: bool, env=None) -> list[Dir]:
    path = hosts.find_config(path_opt, "SSH_DIRS", DIRS_NAME, env)
    found = read_dirs(path)
    if not found:
        raise hosts.HostsError(f"no [[dir]] entries in {path}")
    if pick_dirs:
        chosen = set(pick.pick([d.path for d in found], "dirs"))
        found = [d for d in found if d.path in chosen]
    return found


def dirs_script(dir_list: list[Dir], cmd: str, clone: bool = False) -> str:
    """cmd in each directory in turn, cloning missing ones first when asked.
    POSIX sh on purpose: parallel runs it through whichever shell the host
    has."""
    calls = []
    for d in dir_list:
        words = [d.path]
        if clone and d.url:
            words.append(d.url)
            if d.branch:
                words.append(d.branch)
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
        if opts.dirs:
            cmd = dirs_script(dirs(opts.dirfile, opts.pick_dirs), cmd, opts.clone)
        cmd = shell_command(cmd, opts.interactive)
        entries = hosts.hosts(opts.hosts)
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
