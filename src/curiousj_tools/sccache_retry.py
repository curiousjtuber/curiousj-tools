"""sccache_retry -- a RUSTC_WRAPPER: sccache first, rustc itself when that fails.

    RUSTC_WRAPPER=... cargo build       (cargo runs it as: sccache_retry RUSTC ARG...)

A distributed compile can fail where a local one would not: a proc macro that
reads a file of its crate at compile time (wayland-scanner's wayland.xml) finds
it missing on the build server, and sccache reports that as the compile's own
error, with no local retry. So the compile goes through sccache with its output
held back, and when it fails, rustc runs again here, as cargo asked, its output
the one cargo sees. A real error costs a second, local compile; each retry is
noted in ~/.cache/distbuild/sccache-retries.log (under XDG_CACHE_HOME when set).
A compile reading its source from stdin ("-") goes to rustc directly: its input
could not be given twice, and sccache does not cache it.

A "target-cpu=native" is replaced by the CPU rustc takes it for here, before
sccache sees it: on a build server it would mean that server's CPU, and the
code built there would be for a machine other than this one.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time


def log_path() -> str:
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(cache, "distbuild", "sccache-retries.log")


def crate_name(args: list[str]) -> str:
    for i, arg in enumerate(args):
        if arg == "--crate-name" and i + 1 < len(args):
            return args[i + 1]
    return "?"


def note_retry(args: list[str], status: int, stderr: bytes) -> None:
    last = next((line for line in reversed(stderr.decode(errors="replace").splitlines())
                 if line.strip()), "")
    try:
        os.makedirs(os.path.dirname(log_path()), exist_ok=True)
        with open(log_path(), "a") as f:
            f.write(f"{time.strftime('%F %T')} {crate_name(args)}: sccache exit {status}: {last[:300]}\n")
    except OSError:
        pass


_NATIVE = re.compile(r"((?:-C|--codegen=?)?target-cpu=)native")


def native_cpu(rustc: str) -> str | None:
    """What rustc takes target-cpu=native for on this machine."""
    try:
        out = subprocess.run([rustc, "--print", "target-cpus"], capture_output=True,
                             text=True).stdout
    except OSError:
        return None
    m = re.search(r"^\s*native\b.*\(currently (\S+?)\)", out, re.M)
    return m.group(1) if m else None


def name_native(args: list[str]) -> list[str]:
    """args with each target-cpu=native naming this machine's CPU."""
    if not any(_NATIVE.search(a) for a in args[1:]):
        return args
    cpu = native_cpu(args[0])
    if cpu is None:
        return args
    return [args[0]] + [_NATIVE.sub(rf"\g<1>{cpu}", a) for a in args[1:]]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: sccache_retry RUSTC [ARG...]", file=sys.stderr)
        return 2
    sccache = os.environ.get("DISTBUILD_SCCACHE") or shutil.which("sccache")
    if sccache is None or "-" in args[1:]:
        os.execvp(args[0], args)
    proc = subprocess.run([sccache, *name_native(args)], capture_output=True)
    if proc.returncode == 0:
        sys.stdout.buffer.write(proc.stdout)
        sys.stderr.buffer.write(proc.stderr)
        return 0
    note_retry(args, proc.returncode, proc.stderr)
    sys.stdout.flush()
    os.execvp(args[0], args)
    return 127  # not reached


if __name__ == "__main__":
    sys.exit(main())
