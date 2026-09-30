"""ec-magit -- open magit-status in whichever Emacs emacsclient-auto reaches.

With an `sshtsf -e` forward live, that is the local machine's Emacs, which
then opens the repository on this host through TRAMP.

    ec-magit [-n|--no-frame] [DIR...]

Each DIR (default ".") opens in a new frame of its own, as that frame's only
window. -n opens the magit buffer in the selected frame instead.

The TRAMP prefix goes on the directory only when the call really goes to the
forwarded Emacs. With the forward gone the local Emacs gets the plain path,
where a prefixed one would have it ssh back into this same host.

Takes emacsclient-auto's knobs: EMACSCLIENT_TRAMP_PREFIX,
EMACSCLIENT_FORWARD_SOCKET, EMACSCLIENT_BIN and EMACSCLIENT_AUTO_DEBUG.
"""

from __future__ import annotations

import os
import subprocess
import sys

import click

from . import cmdline
from . import emacsclient_auto as eca


def elisp_string(s: str) -> str:
    """s as an elisp string literal: backslash first, then double quote."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# Each of these was learned the hard way:
#  - `non-essential' must be nil. emacsclient -e evaluates with it non-nil,
#    which tells TRAMP not to open connections for "speculative" work; TRAMP
#    then reports every remote file as nonexistent and magit silently does
#    nothing -- no error, no buffer.
#  - Create and select the frame BEFORE calling magit.
#    magit-status-setup-buffer displays via display-buffer, which acts on
#    whichever frame is selected at the time -- build the buffer first and it
#    lands in the previous frame.
#  - make-frame fails in a headless daemon ("Unknown terminal type"), so fall
#    back to the selected frame there instead of aborting.
def elisp_status(directory: str, frame: bool) -> str:
    """The form that opens magit-status for directory, as Emacs names it."""
    d = elisp_string(directory)
    if not frame:
        return (f"(let ((non-essential nil) (default-directory {d}))"
                f" (magit-status-setup-buffer default-directory) t)")
    return ("(let* ((non-essential nil)"
            " (frame (or (condition-case nil (make-frame) (error nil))"
            " (selected-frame))))"
            " (select-frame-set-input-focus frame)"
            " (with-selected-frame frame"
            f" (let ((default-directory {d}))"
            " (magit-status-setup-buffer default-directory)"
            " (delete-other-windows (frame-selected-window frame))))"
            " t)")


@click.command(cls=cmdline.Command, help=__doc__)
@click.option("-n", "--no-frame", is_flag=True,
              help="Use the selected frame instead of a new one.")
@click.argument("dirs", metavar="[DIR...]", nargs=-1)
def cli(no_frame: bool, dirs: tuple[str, ...]) -> int:
    # All of them before any opens, so a typo does not leave half the frames.
    paths = []
    for arg in dirs or (".",):
        if not os.path.isdir(arg):
            print(f"ec-magit: no such directory: {arg}", file=sys.stderr)
            return cmdline.EXIT_ERROR
        paths.append(os.path.realpath(arg))

    env = dict(os.environ)
    real = eca.find_emacsclient(env)
    if not real:
        print("ec-magit: could not find the real emacsclient on PATH", file=sys.stderr)
        return 127

    # emacsclient-auto's decision, taken once for every directory, and kept
    # to know whether the prefix applies; going through it would hide that.
    socket, prefix = eca.settings(env)
    live = bool(prefix) and eca.socket_live(socket, real, env)
    branch, extra = eca.route(["-e"], socket, prefix, live)
    if branch == "local":
        prefix = ""
    if env.get("EMACSCLIENT_AUTO_DEBUG"):
        note = f"remote via {socket} (prefix {prefix})" if prefix else f"local ({real})"
        print(f"ec-magit: {note}", file=sys.stderr)
    env.update(extra)

    for path in paths:
        form = elisp_status(f"{prefix}{path}/", frame=not no_frame)
        try:
            proc = subprocess.run([real, "-e", form], env=env, stdout=subprocess.DEVNULL)
        except OSError as exc:
            print(f"ec-magit: cannot run {real}: {exc}", file=sys.stderr)
            return 126
        if proc.returncode != 0:
            print(f"ec-magit: failed to open {path}", file=sys.stderr)
            return cmdline.EXIT_ERROR
    return 0


def main(argv: list[str] | None = None) -> int:
    return cmdline.run(cli, argv, "ec-magit")


if __name__ == "__main__":
    sys.exit(main())
