"""podman_docker: sccache-dist's docker builder commands, adapted for podman."""

from __future__ import annotations

import gzip
import io
import os
import tarfile
import tempfile
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

    def test_diff_as_the_builder_can_reclaim_by(self):
        after_job = ("A /etc\nC /home/alice\nC /home\nA /home/alice/.cache\nA /home/alice/.cache/a.rs\n"
                     "C /tmp\nA /tmp/t\n")
        dirs = {"/home", "/home/alice", "/tmp"}
        self.assertEqual(podman_docker.filter_diff(after_job, dirs),
                         "A /home/alice/.cache\nA /home/alice/.cache/a.rs\nA /tmp/t\n")
        after_cleanup = "A /etc\nC /home/alice\nC /tmp\n"
        self.assertEqual(podman_docker.filter_diff(after_cleanup, dirs), "")

    def test_a_changed_file_is_kept(self):
        self.assertEqual(podman_docker.filter_diff("C /opt/tc/bin/rustc\n", set()), "C /opt/tc/bin/rustc\n")

    def test_listings_lose_localhost(self):
        self.assertEqual(podman_docker.strip_localhost(
            "abc localhost/sccache-builder-1\ndef docker.io/aidanhs/busybox\n"),
            "abc sccache-builder-1\ndef docker.io/aidanhs/busybox\n")


class TestLocalize(unittest.TestCase):
    CLIENT = {
        "usr/lib/libc.so.6": b"\x7fELF client libc",
        "usr/lib/libz.so.1.3.1": b"\x7fELF client zlib",
        "opt/tc/bin/rustc": b"\x7fELF client rustc",
        "opt/tc/lib/rustlib/x86_64-unknown-linux-gnu/lib/libstd-1.so": b"\x7fELF target std",
        "opt/tc/lib/rustlib/x86_64-unknown-linux-gnu/lib/libcore-1.rlib": b"!<arch> target core",
        "usr/lib/README": b"not a program",
    }
    SERVER = {
        "usr/lib/libc.so.6": b"\x7fELF server libc, longer than the client's",
        "usr/lib/libz.so.1.3.1": b"\x7fELF server zlib",
        "opt/tc/lib/rustlib/x86_64-unknown-linux-gnu/lib/libstd-1.so": b"\x7fELF server target std",
        "usr/lib/README": b"server text",
    }

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        for name, data in self.SERVER.items():
            path = os.path.join(self.root, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)

    def contents(self, data: bytes) -> dict[str, bytes]:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            return {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}

    def test_runnable_files_become_this_machines(self):
        out = io.BytesIO()
        swapped = podman_docker.localize(io.BytesIO(tar_bytes(**self.CLIENT)), out, self.root)
        self.assertEqual(sorted(swapped), ["usr/lib/libc.so.6", "usr/lib/libz.so.1.3.1"])
        got = self.contents(out.getvalue())
        self.assertEqual(got["usr/lib/libc.so.6"], self.SERVER["usr/lib/libc.so.6"])
        # Not here, a target's library, not a program: as the client sent them.
        for name in ("opt/tc/bin/rustc", "opt/tc/lib/rustlib/x86_64-unknown-linux-gnu/lib/libstd-1.so",
                     "opt/tc/lib/rustlib/x86_64-unknown-linux-gnu/lib/libcore-1.rlib", "usr/lib/README"):
            self.assertEqual(got[name], self.CLIENT[name])

    def test_gzip_on_an_open_pipe(self):
        pipe = OpenPipe(gzip.compress(tar_bytes(**self.CLIENT)))
        self.addCleanup(pipe.close)
        out = io.BytesIO()
        done = threading.Event()
        result = []

        def go():
            result.append(podman_docker.copy_toolchain(pipe.reader, out, self.root))
            done.set()
        threading.Thread(target=go, daemon=True).start()
        self.assertTrue(done.wait(5), "copy_toolchain waited for the pipe to close")
        self.assertEqual(len(result[0]), 2)
        self.assertEqual(set(self.contents(out.getvalue())), set(self.CLIENT))


if __name__ == "__main__":
    unittest.main()
