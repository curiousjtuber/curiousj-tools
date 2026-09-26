"""podman_docker: sccache-dist's docker builder commands, adapted for podman."""

from __future__ import annotations

import gzip
import io
import os
import tarfile
import threading
import unittest

from curiousj_tools import podman_docker


def tar_bytes(**files: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class OpenPipe:
    """The archive on a pipe whose write end stays open, as sccache-dist
    leaves it: reading past the archive would block for good."""

    def __init__(self, data: bytes):
        r, w = os.pipe()
        self.reader = os.fdopen(r, "rb")
        self.writer = os.fdopen(w, "wb")
        threading.Thread(target=self._feed, args=(data,), daemon=True).start()

    def _feed(self, data: bytes) -> None:
        self.writer.write(data)
        self.writer.flush()

    def close(self) -> None:
        self.writer.close()
        self.reader.close()


class TestCopyArchive(unittest.TestCase):
    def copy(self, data: bytes) -> bytes:
        pipe = OpenPipe(data)
        self.addCleanup(pipe.close)
        out = io.BytesIO()
        done = threading.Event()

        def go():
            podman_docker.copy_archive(pipe.reader, out)
            done.set()
        threading.Thread(target=go, daemon=True).start()
        self.assertTrue(done.wait(5), "copy_archive waited for the pipe to close")
        return out.getvalue()

    def names(self, data: bytes) -> list[str]:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            return tar.getnames()

    def test_tar_stops_at_its_end(self):
        data = tar_bytes(**{"a.txt": b"x" * 1000, "b/c.txt": b"y"})
        self.assertEqual(self.names(self.copy(data)), ["a.txt", "b/c.txt"])

    def test_gzip_stops_at_the_member_end(self):
        data = gzip.compress(tar_bytes(**{"lib/rustc": b"z" * 70000}))
        self.assertEqual(self.names(self.copy(data)), ["lib/rustc"])

    def test_short_input(self):
        src = io.BufferedReader(io.BytesIO(b"x"))
        out = io.BytesIO()
        podman_docker.copy_archive(src, out)
        self.assertEqual(out.getvalue(), b"x")


class TestArgs(unittest.TestCase):
    def test_rm_does_not_wait(self):
        self.assertEqual(podman_docker.podman_args(["rm", "-f", "c1", "c2"]),
                         ["rm", "-f", "-t", "0", "c1", "c2"])

    def test_others_unchanged(self):
        for args in (["exec", "c1", "/busybox", "true"], ["rmi", "i1"], ["cp", "-", "c1:/"]):
            self.assertEqual(podman_docker.podman_args(args), args)

    def test_listings_lose_localhost(self):
        self.assertEqual(podman_docker.strip_localhost(
            "abc localhost/sccache-builder-1\ndef docker.io/aidanhs/busybox\n"),
            "abc sccache-builder-1\ndef docker.io/aidanhs/busybox\n")


if __name__ == "__main__":
    unittest.main()
