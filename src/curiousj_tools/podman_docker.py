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
  diff CID     after a job the builder reuses the container only when its diff
               is empty once the job's added files are gone; it gave up on
               podman's own /etc (hosts, resolv.conf, mounted in) and on a
               directory the job's files were added under, marked changed
               ("C /home/USER" when the toolchain and the build both live in
               a home). Those are left out; a changed file stays in.

A toolchain copied in -- into a container of the builder's base image --
is also made to run here. The client packs its compiler with its own copies
of the libraries it loads, and those may be built for a newer CPU than this
server's (CachyOS's znver4 glibc on an older Zen). Each ELF program or
library in it that this machine also has at the same path is replaced by
this machine's; a target's libraries (rustlib/TARGET/lib) are not, as they
are what the compile links against, not what runs it.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tarfile
import threading
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


BASE_IMAGE = "docker.io/aidanhs/busybox"
_TARGET_LIBS = re.compile(r"(^|/)rustlib/[^/]+/lib/")


def is_elf(data: bytes) -> bool:
    return data[:4] == b"\x7fELF"


def local_twin(name: str, root: str = "/") -> str | None:
    """This machine's file at the archive member name's path, when it is a
    runnable ELF file and not a target's library."""
    if _TARGET_LIBS.search(name):
        return None
    path = os.path.join(root, name.lstrip("/"))
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return None
    return path if is_elf(head) else None


def localize(src, out, root: str = "/") -> list[str]:
    """The tar on src to out, each ELF member that has a local twin replaced
    by it. What was replaced."""
    swapped = []
    with tarfile.open(fileobj=src, mode="r|") as tin, tarfile.open(fileobj=out, mode="w|") as tout:
        for m in tin:
            data = tin.extractfile(m) if m.isfile() else None
            if data is not None:
                head = data.read(4)
                twin = local_twin(m.name, root) if is_elf(head) else None
                if twin:
                    with open(twin, "rb") as f:
                        m.size = os.fstat(f.fileno()).st_size
                        tout.addfile(m, f)
                    swapped.append(m.name)
                    continue
                tout.addfile(m, io.BufferedReader(_Rejoined(head, data), _CHUNK))
            else:
                tout.addfile(m)
    return swapped


class _Rejoined(io.RawIOBase):
    """head, then the rest of stream: a member whose first bytes were read."""

    def __init__(self, head: bytes, stream):
        self.head, self.stream = head, stream

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        if self.head:
            n = min(len(b), len(self.head))
            b[:n], self.head = self.head[:n], self.head[n:]
            return n
        chunk = self.stream.read(len(b))
        b[:len(chunk)] = chunk
        return len(chunk)


def copy_toolchain(src, out, root: str = "/") -> list[str]:
    """One archive from src, as copy_archive reads it, to out localized."""
    r, w = os.pipe()
    reader, writer = os.fdopen(r, "rb"), os.fdopen(w, "wb")

    def feed():
        try:
            copy_archive(src, writer)
        finally:
            writer.close()
    t = threading.Thread(target=feed, daemon=True)
    t.start()
    try:
        return localize(reader, out, root)
    finally:
        reader.close()
        t.join()


def is_base_container(cid: str) -> bool:
    out = subprocess.run(["podman", "inspect", "--format", "{{.ImageName}}", cid],
                         capture_output=True, text=True).stdout.strip()
    return out.split(":")[0] == BASE_IMAGE


def filter_diff(diff: str, dirs: set[str]) -> str:
    """podman's diff as the builder can reclaim a container by it: podman's
    /etc and changed directories (dirs) left out, sorted by path, as the
    builder assumes docker's to be."""
    kept = []
    for line in diff.splitlines():
        kind, _, path = line.partition(" ")
        if path == "/etc" or path.startswith("/etc/"):
            continue
        if kind == "C" and path in dirs:
            continue
        kept.append((path, line))
    return "".join(line + "\n" for _, line in sorted(kept))


def changed_dirs(cid: str, diff: str) -> set[str]:
    """The paths the diff has as changed that are directories in cid."""
    changed = [line[2:] for line in diff.splitlines() if line.startswith("C ")]
    if not changed:
        return set()
    out = subprocess.run(["podman", "exec", cid, "/busybox", "sh", "-c",
                          'for p; do [ -d "$p" ] && echo "$p"; done', "_", *changed],
                         capture_output=True, text=True).stdout
    return set(out.splitlines())


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
        if len(args) > 2 and is_base_container(args[2].split(":")[0]):
            swapped = copy_toolchain(sys.stdin.buffer, proc.stdin)
            print(f"podman_docker: toolchain made to run here, {len(swapped)} files of this "
                  f"machine's: {' '.join(swapped)}", file=sys.stderr)
        else:
            copy_archive(sys.stdin.buffer, proc.stdin)
        proc.stdin.close()
        return proc.wait()
    if args[:1] == ["diff"] and len(args) == 2:
        proc = subprocess.run(["podman", *args], stdout=subprocess.PIPE, text=True)
        if proc.returncode == 0:
            sys.stdout.write(filter_diff(proc.stdout, changed_dirs(args[1], proc.stdout)))
        return proc.returncode
    if args[:1] in (["ps"], ["images"]) and "--format" in args:
        proc = subprocess.run(["podman", *args], stdout=subprocess.PIPE, text=True)
        sys.stdout.write(strip_localhost(proc.stdout))
        return proc.returncode
    os.execvp("podman", ["podman", *args])
    return 127  # not reached


if __name__ == "__main__":
    sys.exit(main())
