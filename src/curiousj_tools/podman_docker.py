"""podman_docker -- the `docker` sccache-dist's docker builder calls, run by podman.

`distbuild sccache` puts a `docker` script first on the build server's PATH
that runs this. Three of the builder's commands behave differently under
podman, and are adapted; the rest go to podman as they are:

  cp - CID:/   sccache-dist writes a tar into the pipe but holds it open
               while it waits for the command to end. docker stops at the
               archive's end, podman reads to end of file, and the two wait
               on each other. One archive is read -- a gzip member, or a tar
               to its two zero blocks -- and podman's input then closed.
  rm -f        the containers run busybox sh as pid 1, which ignores
               SIGTERM; podman would wait its 10 s before killing each one.
  ps/images    podman names local images "localhost/NAME", and the builder's
    --format   startup cleanup looks for names starting "sccache-builder-".
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import zlib

BLOCK = 512
_CHUNK = 1 << 16


def _read_some(src, n: int = _CHUNK) -> bytes:
    """What is available, up to n bytes, without waiting for more: sccache
    never closes the pipe, so a full read would wait for good."""
    return src.read1(n)


def copy_gzip_member(src, first: bytes, out) -> None:
    """One gzip member from src, decompressed, into out."""
    d = zlib.decompressobj(wbits=31)
    out.write(d.decompress(first))
    while not d.eof:
        chunk = _read_some(src)
        if not chunk:
            break
        out.write(d.decompress(chunk))


def copy_tar(src, first: bytes, out) -> None:
    """One tar archive from src into out, through its two zero blocks."""
    buf, zeros = first, 0

    def block() -> bytes:
        nonlocal buf
        while len(buf) < BLOCK:
            chunk = _read_some(src)
            if not chunk:
                break
            buf += chunk
        b, buf = buf[:BLOCK], buf[BLOCK:]
        return b

    while True:
        b = block()
        out.write(b)
        if len(b) < BLOCK:
            return
        if b == bytes(BLOCK):
            zeros += 1
            if zeros == 2:
                return
            continue
        zeros = 0
        size = int(b[124:136].rstrip(b"\0 ") or b"0", 8)
        for _ in range((size + BLOCK - 1) // BLOCK):
            out.write(block())


def copy_archive(src, out) -> None:
    first = b""
    while len(first) < 2:
        chunk = _read_some(src, 2 - len(first))
        if not chunk:
            break
        first += chunk
    if first[:2] == b"\x1f\x8b":
        copy_gzip_member(src, first, out)
    else:
        copy_tar(src, first, out)


def podman_args(args: list[str]) -> list[str]:
    if args[:2] == ["rm", "-f"]:
        return ["rm", "-f", "-t", "0", *args[2:]]
    return args


def strip_localhost(listing: str) -> str:
    return re.sub(r"(^|\s)localhost/", r"\1", listing, flags=re.M)


def main(argv: list[str] | None = None) -> int:
    args = podman_args(sys.argv[1:] if argv is None else argv)
    if args[:2] == ["cp", "-"]:
        proc = subprocess.Popen(["podman", *args], stdin=subprocess.PIPE)
        copy_archive(sys.stdin.buffer, proc.stdin)
        proc.stdin.close()
        return proc.wait()
    if args[:1] in (["ps"], ["images"]) and "--format" in args:
        proc = subprocess.run(["podman", *args], stdout=subprocess.PIPE, text=True)
        sys.stdout.write(strip_localhost(proc.stdout))
        return proc.returncode
    os.execvp("podman", ["podman", *args])
    return 127  # not reached


if __name__ == "__main__":
    sys.exit(main())
