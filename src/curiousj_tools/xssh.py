"""xssh -- synchronized xpanes window: one ssh pane per listed host, plus a local shell.

    xssh [-h] [-N|--no-local] [-p|--pick] [-f FILE] [xpanes-options...]

One tmux pane per host, each running `ssh host`, with synchronize-panes on
so one line typed lands in every shell. A local shell gets a pane too, so
the same command also hits this machine. The host list and -N, -p, -f are
ssh-hosts' (see `ssh-hosts -h`); any other argument is passed through to
xpanes (`--stay`, `-l ev`, ...). pssh is the batch counterpart.

Each pane gets a login shell, so unlike pssh the remote side reads its
shell rc: aliases work and `cd` persists between commands. A host with
`commands` in the lists file runs them after login instead of stopping at
the shell -- `ssh -t HOST 'cd src; exec zsh'`, or `distrobox enter dev` --
so the last one should be what you want to type into: an interactive
shell, a container entered. The pane ends when it exits.

A host whose key is not in known_hosts yet is contacted once beforehand,
so ssh's yes/no question is answered here rather than in a pane, where a
synchronized "yes" would reach every other pane as a command.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys

import click

from . import hosts, pick
from .lists import HostInfo

# The local pane is `cd ~; exec $SHELL` rather than `cd ~ && exec $SHELL`:
# xpanes substitutes arguments with bash's ${cmd//{}/arg}, and since bash 5.2
# an `&` in the replacement stands for the matched text, so `&&` arrives as
# `{}{}`. exec so it is a fresh shell at ~ like the remote ones, not the
# shell xpanes typed the command into.
LOCAL_PANE = "cd ~; exec $SHELL"


def pane_command(entry: HostInfo) -> str:
    if entry.host == "localhost":
        return LOCAL_PANE
    if not entry.commands:
        return f"ssh {entry.host}"
    return "ssh -t %s %s" % (entry.host, shlex.quote("; ".join(entry.commands)))


def xpanes_expands_ampersand() -> bool:
    """Whether the bash xpanes runs under makes `&` in a substituted argument
    stand for the matched text (patsub_replacement, on by default since
    bash 5.2). `shopt -q` fails on a bash too old to know the option."""
    try:
        return subprocess.run(["bash", "-c", "shopt -q patsub_replacement"],
                              capture_output=True).returncode == 0
    except OSError:
        return False


def for_xpanes(cmd: str, ampersand: bool) -> str:
    """cmd as xpanes should be handed it: with `&` and backslash escaped for
    the substitution described at LOCAL_PANE, when that bash expands them."""
    return cmd.replace("\\", "\\\\").replace("&", "\\&") if ampersand else cmd


def pane_commands(entries: list[HostInfo], ampersand: bool = False) -> list[str]:
    return [for_xpanes(pane_command(e), ampersand) for e in entries]


@click.command(cls=hosts.Command, help=__doc__,
               context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
@hosts.host_options
@click.argument("xpanes_args", nargs=-1, type=click.UNPROCESSED)
def cli(opts: hosts.HostOpts, xpanes_args: tuple[str, ...]) -> int:
    """Host flags anywhere in argv are ours; the rest go to xpanes in order."""
    try:
        if not shutil.which("xpanes"):
            raise hosts.HostsError("xpanes not installed (https://github.com/greymd/tmux-xpanes)")
        entries = hosts.hosts(opts)
        hosts.confirm_new_hosts([e.host for e in entries], "xssh")
    except hosts.HostsError as e:
        print(f"xssh: {e}", file=sys.stderr)
        return hosts.EXIT_ERROR
    except pick.Abort:
        return pick.EXIT_ABORT
    ampersand = any(e.commands for e in entries) and xpanes_expands_ampersand()
    os.execvp("xpanes", ["xpanes", *xpanes_args, "-e", *pane_commands(entries, ampersand)])
    return 0


def main(argv: list[str] | None = None) -> int:
    return hosts.run(cli, argv, "xssh")


if __name__ == "__main__":
    sys.exit(main())
