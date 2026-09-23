"""curiousj-tools -- the shell side of the other commands.

    curiousj-tools init [--path] bash|zsh

'init' prints shell code for an rc file to eval, after the directory the
commands were installed in is on PATH:

    eval "$(curiousj-tools init zsh)"     # ~/.zshrc
    eval "$(curiousj-tools init bash)"    # ~/.bashrc

It defines 'emacs-remote', which routes emacsclient, $EDITOR and $BROWSER
through emacsclient-auto and ec-browse to the Emacs `sshtsf -e` forwarded, and
'tmux-env-refresh', a prompt hook that re-reads WAYLAND_DISPLAY,
SSH_AUTH_SOCK, EMACS_REMOTE_TARGET and the like from tmux after a reattach
(TMUX_ENV_REFRESH_EXTRA names more, separated by spaces). On an interactive
ssh login it turns emacs-remote on. --path prints where the file is instead,
for an rc file that would rather `source` it than start python each time.
"""

from __future__ import annotations

import sys
from importlib import resources

import click

from . import cmdline

SHELLS = ("bash", "zsh")


def script_file():
    """The shell file this package ships; the same one serves both shells."""
    return resources.files(__package__) / "shell" / "curiousj-tools.sh"


@click.group(cls=cmdline.Group, help=__doc__)
def cli() -> None:
    pass


@cli.command(cls=cmdline.Command, help=__doc__)
@click.option("--path", "path", is_flag=True, help="Print the file's path, not its contents.")
@click.argument("shell", type=click.Choice(SHELLS))
def init(shell: str, path: bool) -> int:
    # SHELL is checked, not used: the script tells bash from zsh itself, and
    # taking it keeps the `mise activate zsh` shape should the two diverge.
    f = script_file()
    if path:
        click.echo(str(f))
    else:
        sys.stdout.write(f.read_text())
    return 0


def main(argv: list[str] | None = None) -> int:
    return cmdline.run(cli, argv, "curiousj-tools")


if __name__ == "__main__":
    sys.exit(main())
