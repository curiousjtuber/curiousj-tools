"""emacsclient-auto -- route emacsclient to a forwarded Emacs when its socket
is live, otherwise fall back to the local Emacs server.

Used both as $EDITOR (git execs it directly, so it must be a real executable,
not a shell function) and behind an `emacsclient` shell function or alias that
points at it. The forwarded socket is what `sshtsf -e` sets up from the
other end: it reverse-forwards the local machine's Emacs server socket to
EMACSCLIENT_FORWARD_SOCKET on the remote, and this script, running on the
remote, notices it and talks to that Emacs instead of a local one.

Env knobs (all optional):
  EMACSCLIENT_TRAMP_PREFIX   e.g. /ssh:user@devbox:  -- empty => never route remote
  EMACSCLIENT_FORWARD_SOCKET forwarded socket path  -- default /tmp/emacs-remote-socket-USER
  EMACSCLIENT_BIN            explicit path to the real emacsclient
  EMACSCLIENT_AUTO_DEBUG=1   print which branch was taken, to stderr

When routing remote, EMACS_SOCKET_NAME is set to the forwarded socket and
EMACSCLIENT_TRAMP to the prefix, so the real emacsclient rewrites file
arguments into TRAMP paths that the far Emacs can open back over ssh.
"""

from __future__ import annotations

import getpass
import os
import stat
import subprocess
import sys


# Where sshtsf lands its forward and this looks for it, less the -USER.
# sshtsf imports it, so the two ends cannot drift apart.
SOCKET_PREFIX = "/tmp/emacs-remote-socket"


def login_name() -> str:
    """The login this process runs as; its uid when it has no passwd entry."""
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return str(os.getuid())


def default_socket() -> str:
    """/tmp/emacs-remote-socket-USER: per login, as sshtsf names its forward.

    The suffix is the login this process runs as, which is what sshtsf's
    `id -un' on the remote answered when it set the forward up; two users on
    one host thus get two sockets in the sticky /tmp instead of a fight over
    one.
    """
    return "%s-%s" % (SOCKET_PREFIX, login_name())


DEFAULT_SOCKET = default_socket()

# Names this tool may be installed or symlinked under; a PATH entry called
# `emacsclient` that resolves to one of these is us, not the real client.
SELF_NAMES = ("emacsclient-auto", "emacsclient-auto.sh", "emacsclient_auto.py")

PROBE_TIMEOUT = 2


def self_paths() -> set[str]:
    """Canonical paths that mean "this program", for the recursion guard."""
    paths = {os.path.realpath(__file__)}
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        paths.add(os.path.realpath(argv0))
    return paths


def find_emacsclient(env: dict, self: set[str] | None = None) -> str | None:
    """The real emacsclient: EMACSCLIENT_BIN, else the first on PATH that is
    not this program (an `emacsclient` symlink pointing back here would
    otherwise recurse forever)."""
    explicit = env.get("EMACSCLIENT_BIN")
    if explicit:
        return explicit
    if self is None:
        self = self_paths()
    for d in (env.get("PATH") or "").split(os.pathsep):
        cand = os.path.join(d or ".", "emacsclient")
        if os.path.isdir(cand) or not os.access(cand, os.X_OK):
            continue
        canon = os.path.realpath(cand)
        if canon in self or os.path.basename(canon) in SELF_NAMES:
            continue
        return cand
    return None


def is_eval(args: list[str]) -> bool:
    """Whether this is an --eval call. EMACSCLIENT_TRAMP rewrites *file*
    arguments into TRAMP paths but hangs --eval invocations, so those go over
    the socket without the prefix: an eval string carries its own paths."""
    return any(a in ("-e", "--eval") or a.startswith("--eval=") for a in args)


def is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def socket_live(socket: str, real: str, env: dict) -> bool:
    """Whether an Emacs actually answers on socket. An ungraceful disconnect
    leaves a stale socket file behind, so existence alone is not enough."""
    if not is_socket(socket):
        return False
    probe_env = dict(env)
    probe_env["EMACS_SOCKET_NAME"] = socket
    try:
        proc = subprocess.run([real, "-e", "t"], env=probe_env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=PROBE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def settings(env: dict) -> tuple[str, str]:
    """The forwarded socket and the TRAMP prefix, as the env knobs set them."""
    return (env.get("EMACSCLIENT_FORWARD_SOCKET") or DEFAULT_SOCKET,
            env.get("EMACSCLIENT_TRAMP_PREFIX") or "")


def route(args: list[str], socket: str, prefix: str,
          live: bool) -> tuple[str, dict[str, str]]:
    """Decide the branch: ("local" | "remote" | "remote-eval", env additions).

    Remote only when the server answered AND a TRAMP prefix is set: without a
    prefix a file argument could not be rewritten for the far Emacs, so the
    forward is of no use to a file-visiting call.
    """
    if live and prefix:
        if is_eval(args):
            return "remote-eval", {"EMACS_SOCKET_NAME": socket}
        return "remote", {"EMACS_SOCKET_NAME": socket, "EMACSCLIENT_TRAMP": prefix}
    return "local", {}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    env = dict(os.environ)
    real = find_emacsclient(env)
    if not real:
        print("emacsclient-auto: could not find the real emacsclient on PATH",
              file=sys.stderr)
        return 127

    socket, prefix = settings(env)
    # The probe costs a round trip, so it is skipped when the answer could not
    # change the branch anyway.
    live = bool(prefix) and socket_live(socket, real, env)
    branch, extra = route(args, socket, prefix, live)

    if env.get("EMACSCLIENT_AUTO_DEBUG"):
        if branch == "remote-eval":
            note = "remote eval via %s (no tramp prefix)" % socket
        elif branch == "remote":
            note = "remote via %s (prefix %s)" % (socket, prefix)
        else:
            note = "local (%s)" % real
        print("emacsclient-auto: %s" % note, file=sys.stderr)

    env.update(extra)
    try:
        os.execvpe(real, [real] + args, env)
    except OSError as exc:
        print("emacsclient-auto: cannot exec %s: %s" % (real, exc), file=sys.stderr)
        return 126
    return 0  # pragma: no cover - exec does not return


if __name__ == "__main__":
    sys.exit(main())
