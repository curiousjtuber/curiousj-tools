"""xssh -- synchronized xpanes window: one ssh pane per listed host, plus a local shell.

    xssh [-h] [-N|--no-local] [-p|--pick] [-f HOSTFILE] [xpanes-options...]

One tmux pane per host, each running `ssh host`, with synchronize-panes on
so one line typed lands in every shell. A local shell gets a pane too, so
the same command also hits this machine. The host list and -N, -p, -f are
ssh-hosts' (see `ssh-hosts -h`); any other argument is passed through to
xpanes (`--stay`, `-l ev`, ...). pssh is the batch counterpart.

Each pane gets a login shell, so unlike pssh the remote side reads its
shell rc: aliases work and `cd` persists between commands.

A host whose key is not in known_hosts yet is contacted once beforehand,
so ssh's yes/no question is answered here rather than in a pane, where a
synchronized "yes" would reach every other pane as a command.
"""

from __future__ import annotations

import os
import shutil
import sys

from . import hosts, pick

# The local pane is `cd ~; exec $SHELL` rather than `cd ~ && exec $SHELL`:
# xpanes substitutes arguments with bash's ${cmd//{}/arg}, and since bash 5.2
# an `&` in the replacement stands for the matched text, so `&&` arrives as
# `{}{}`. exec so it is a fresh shell at ~ like the remote ones, not the
# shell xpanes typed the command into.
LOCAL_PANE = "cd ~; exec $SHELL"


def split_args(argv: list[str]) -> tuple[hosts.HostOpts, list[str]]:
    """Host flags anywhere in argv go to ssh-hosts; the rest go to xpanes."""
    opts = hosts.HostOpts()
    passthrough = []
    i = 0
    while i < len(argv):
        j = hosts.take_host_opt(argv, i, opts, "xssh")
        if j == i:
            passthrough.append(argv[i])
            j = i + 1
        i = j
    return opts, passthrough


def pane_commands(entries: list[str]) -> list[str]:
    return [LOCAL_PANE if e == "localhost" else f"ssh {e}" for e in entries]


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "-h" in argv or "--help" in argv:
        print(__doc__.rstrip())
        return 0
    try:
        opts, xpanes_args = split_args(argv)
        if not shutil.which("xpanes"):
            raise hosts.HostsError("xpanes not installed (https://github.com/greymd/tmux-xpanes)")
        entries = hosts.hosts(opts)
    except hosts.UsageError as e:
        print(f"xssh: {e}", file=sys.stderr)
        return hosts.EXIT_USAGE
    except hosts.HostsError as e:
        print(f"xssh: {e}", file=sys.stderr)
        return hosts.EXIT_ERROR
    except pick.Abort:
        return pick.EXIT_ABORT
    try:
        hosts.confirm_new_hosts(entries, "xssh")
    except hosts.HostsError as e:
        print(f"xssh: {e}", file=sys.stderr)
        return hosts.EXIT_ERROR
    os.execvp("xpanes", ["xpanes", *xpanes_args, "-e", *pane_commands(entries)])


if __name__ == "__main__":
    sys.exit(main())
