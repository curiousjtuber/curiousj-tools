"""xssh -- synchronized xpanes window: one ssh pane per listed login, plus a local shell.

    xssh [-h] [-N|--no-local] [-a TERM]... [-p|--pick] [-f FILE]... [xpanes-options...]
    xssh [same flags] -o NAME... [-A TERM]... [--clone] [--pick-paths] [xpanes-options...]
    xssh [-f FILE]... -L|--list-ops

One tmux pane per login, each running `ssh LOGIN`, with synchronize-panes
on so one line typed lands in every shell. A local shell gets a pane too, so
the same command also hits this machine. The login list and -N, -a, -p, -f
are ssh-logins' (see `ssh-logins -h`); any other argument is passed through
to xpanes ('--stay', '-l ev', ...). pssh is the batch counterpart.

Each pane gets a login shell, so unlike pssh the remote side reads its
shell rc: aliases work and 'cd' persists between commands. A login with
'commands' in the lists file runs them after login instead of stopping at
the shell -- `ssh -t LOGIN 'cd src; exec zsh'`, or `distrobox enter dev -nw` --
so the last one should be what you want to type into: an interactive
shell, a container entered. The pane ends when it exits. The local pane
runs the 'commands' of the entry naming this machine, if it has any (see
`ssh-logins -h`), from ~. (A login's 'via' is pssh's business; the pane
types 'commands' only.)

-o/--op NAME runs an operation instead (see `pssh -h` for what one is, and
the built-in ones): a pane per login the operation applies to, each running
the login's share of it as pssh would -- through its 'via', "== NAME" and
"== DIR" announcing each step -- then dropping into a shell. The panes
being synchronized, a question every login asks is answered once, for all
of them; where hosts ask different questions, `pssh -s` is the better
tool. -A/--path-attr, --clone and --pick-paths are pssh's -A, -c and -C
(the short letters are xpanes'). The pane then runs the login's 'commands',
so it ends where a plain pane would. A login the operation does not apply
to gets no pane. -L/--list-ops lists the operations, as `pssh -L` does.

A login whose host key is not in known_hosts yet is contacted once beforehand,
so ssh's yes/no question is answered here rather than in a pane, where a
synchronized "yes" would reach every other pane as a command.
"""

from __future__ import annotations

import base64
import os
import shlex
import shutil
import subprocess
import sys

import click

from . import attrs, cmdline, lists, logins, pick, pssh
from .lists import Login, ToolError

# The local pane is `cd ~; exec $SHELL` rather than `cd ~ && exec $SHELL`:
# xpanes substitutes arguments with bash's ${cmd//{}/arg}, and since bash 5.2
# an '&' in the replacement stands for the matched text, so "&&" arrives as
# "{}{}". exec so it is a fresh shell at ~ like the remote ones, not the
# shell xpanes typed the command into.
LOCAL_PANE = "cd ~; exec $SHELL"


def local_pane(entry: Login) -> str:
    """The local pane: at ~ like a fresh ssh login, then the commands
    'localhost' took over from the entry naming this machine, else a shell."""
    return "; ".join(["cd ~", *entry.commands]) if entry.commands else LOCAL_PANE


def pane_command(entry: Login) -> str:
    if entry.login == "localhost":
        return local_pane(entry)
    if not entry.commands:
        return f"ssh {entry.login}"
    return f"ssh -t {entry.login} {shlex.quote('; '.join(entry.commands))}"


def xpanes_expands_ampersand() -> bool:
    """Whether the bash xpanes runs under makes '&' in a substituted argument
    stand for the matched text (patsub_replacement, on by default since
    bash 5.2). `shopt -q` fails on a bash too old to know the option."""
    try:
        return subprocess.run(["bash", "-c", "shopt -q patsub_replacement"],
                              capture_output=True).returncode == 0
    except OSError:
        return False


def for_xpanes(cmd: str, ampersand: bool) -> str:
    """cmd as xpanes should be handed it: with '&' and backslash escaped for
    the substitution described at LOCAL_PANE, when that bash expands them."""
    return cmd.replace("\\", "\\\\").replace("&", "\\&") if ampersand else cmd


def pane_commands(entries: list[Login]) -> list[str]:
    return [pane_command(e) for e in entries]


def op_pane_command(entry: Login, script_text: str) -> str:
    """The pane for a login's share of an operation: the script, wrapped as
    pssh runs it (via and all), then what the plain pane would run -- the
    login's commands, else a shell. xpanes types the command into the pane
    with tmux send-keys, so it must hold no newline: the script travels
    base64-encoded and is decoded on the login."""
    line = pssh.wrap(script_text, entry.via)
    encoded = base64.b64encode(line.encode()).decode()
    then = ("; ".join(entry.commands) if entry.commands and entry.login != "localhost"
            else local_pane(entry))
    inner = f'sh -c "$(echo {encoded} | base64 -d)"; {then}'
    if entry.login == "localhost":
        return inner
    return f"ssh -t {entry.login} {shlex.quote(inner)}"


def op_panes(entries: list[Login], ops: list[lists.Operation], path_list: list[lists.PathInfo],
             clone: bool = False) -> tuple[list[Login], list[str], list[str]]:
    """(the logins taking part, their pane commands, the logins left out)."""
    kept, commands, skipped = [], [], []
    for entry in entries:
        script_text = pssh.login_script(entry, ops, path_list, clone)
        if script_text is None:
            skipped.append(entry.login)
            continue
        kept.append(entry)
        commands.append(op_pane_command(entry, script_text))
    return kept, commands, skipped


@click.command(cls=cmdline.Command, help=__doc__,
               context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
@logins.login_options
@click.option("-o", "--op", "ops", metavar="NAME", multiple=True,
              help="a pane per login running the operation NAME (repeatable)")
@click.option("-L", "--list-ops", is_flag=True, help="list the operations, built-in and the lists files'")
@pssh.path_options(short=False, implies=" (with -o)")
@click.argument("xpanes_args", nargs=-1, type=click.UNPROCESSED)
def cli(opts: logins.LoginOpts, ops: tuple[str, ...], list_ops: bool,
        path_attrs: tuple[attrs.Term, ...], clone: bool, pick_paths: bool,
        xpanes_args: tuple[str, ...]) -> int:
    """Login flags anywhere in argv are ours; the rest go to xpanes in order."""
    if (path_attrs or clone or pick_paths) and not ops:
        raise click.UsageError("-A, --clone and --pick-paths go with -o NAME.",
                               click.get_current_context())
    try:
        if list_ops:
            print(pssh.list_operations(lists.load_all(opts.files)))
            return 0
        if not shutil.which("xpanes"):
            raise ToolError("xpanes not installed (https://github.com/greymd/tmux-xpanes)")
        if ops:
            found = lists.load_all(opts.files)
            leaves = pssh.expand(list(ops), found.operations)
            path_list = (pssh.paths_list(found, pick_paths, path_attrs)
                         if any(op.per_path for op in leaves) else [])
            entries, commands, skipped = op_panes(logins.select(opts, found=found), leaves,
                                                 path_list, clone)
            for login in skipped:
                print(f"xssh: {login}: no operation applies, no pane", file=sys.stderr)
            if not entries:
                raise ToolError("no login takes part")
        else:
            entries = logins.select(opts)
            commands = pane_commands(entries)
        # A pane of either kind types the login's commands, the one place an
        # '&' or a backslash comes from, so the bash is asked only then.
        ampersand = any(e.commands for e in entries) and xpanes_expands_ampersand()
        commands = [for_xpanes(c, ampersand) for c in commands]
        logins.confirm_new_hosts([e.login for e in entries], "xssh")
    except ToolError as e:
        print(f"xssh: {e}", file=sys.stderr)
        return cmdline.EXIT_ERROR
    except pick.Abort:
        return pick.EXIT_ABORT
    os.execvp("xpanes", ["xpanes", *xpanes_args, "-e", *commands])
    return 0


def main(argv: list[str] | None = None) -> int:
    return cmdline.run(cli, argv, "xssh")


if __name__ == "__main__":
    sys.exit(main())
