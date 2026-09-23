"""ec-browse -- open URLs in the browser of whichever Emacs emacsclient reaches.

With an `sshtsf -e` forward live, that is the local machine's Emacs, so a
URL printed by a tool on the remote host opens in the local browser.

Suitable as $BROWSER: it takes the URLs as plain arguments, which is what
every $BROWSER consumer does when the value has no '%s' placeholder.

    ec-browse https://example.com [URL...]

Env knobs:
  EC_BROWSE_EMACSCLIENT  emacsclient to use; defaults to `emacsclient-auto`
                         on PATH, else this package's own copy of it
  EC_BROWSE_FUNCTION     elisp function to call; default browse-url.
                         Set to browse-url-default-browser to force the
                         external browser even when browse-url-browser-function
                         is something in-Emacs like eww.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import click

from . import cmdline

DEFAULT_FUNCTION = "browse-url"


def emacsclient_command(env: dict) -> list[str]:
    """The emacsclient to call, as an argv prefix.

    The routing wrapper is preferred so ec-browse follows the same
    local/forwarded decision as $EDITOR does. Its module form is the fallback
    for a checkout that is not installed on PATH; the wrapper itself falls back
    to plain emacsclient when no forward is live.
    """
    explicit = env.get("EC_BROWSE_EMACSCLIENT")
    if explicit:
        return [explicit]
    auto = shutil.which("emacsclient-auto", path=env.get("PATH"))
    if auto:
        return [auto]
    return [sys.executable, "-m", "curiousj_tools.emacsclient_auto"]


def elisp_call(function: str, url: str) -> str:
    """`(browse-url "...")` with the URL escaped as an elisp string literal:
    backslash first, then double quote."""
    escaped = url.replace("\\", "\\\\").replace('"', '\\"')
    return '(%s "%s")' % (function, escaped)


@click.command(cls=cmdline.Command, help=__doc__)
@click.argument("urls", metavar="URL...", nargs=-1, required=True)
def cli(urls: tuple[str, ...]) -> int:
    env = dict(os.environ)
    ec = emacsclient_command(env)
    function = env.get("EC_BROWSE_FUNCTION") or DEFAULT_FUNCTION

    status = 0
    for url in urls:
        # browse-url is not a file-visiting call, so no TRAMP prefix applies:
        # a URL must reach the remote Emacs unprefixed. emacsclient-auto
        # already skips the prefix for -e/--eval.
        try:
            proc = subprocess.run(ec + ["-e", elisp_call(function, url)],
                                  stdout=subprocess.DEVNULL)
            failed = proc.returncode != 0
        except OSError as exc:
            print("ec-browse: cannot run %s: %s" % (ec[0], exc), file=sys.stderr)
            failed = True
        if failed:
            print("ec-browse: failed to open %s" % url, file=sys.stderr)
            status = 1
    return status


def main(argv: list[str] | None = None) -> int:
    return cmdline.run(cli, argv, "ec-browse")


if __name__ == "__main__":
    sys.exit(main())
