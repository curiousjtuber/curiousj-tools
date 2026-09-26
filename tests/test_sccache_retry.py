"""sccache_retry: sccache first, rustc itself when that fails."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"


class RetryCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.calls = self.dir / "calls"
        # rustc: prints its args, exits 0 unless one is "bad"
        self.rustc = self.script("rustc", 'echo "rustc $*"; echo "rustc err" >&2; '
                                          'case " $* " in *" bad "*) exit 3;; esac')

    def script(self, name: str, body: str) -> pathlib.Path:
        p = self.dir / name
        p.write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.calls}"\ncat >/dev/null\n{body}\n'
                     if name == "sccache" else f'#!/bin/sh\necho "{name} $*" >> "{self.calls}"\n{body}\n')
        p.chmod(0o755)
        return p

    def run_retry(self, sccache_body: str, *args: str) -> subprocess.CompletedProcess:
        sccache = self.script("sccache", sccache_body)
        env = dict(os.environ, DISTBUILD_SCCACHE=str(sccache), XDG_CACHE_HOME=str(self.dir / "cache"),
                   PYTHONPATH=str(SRC))
        return subprocess.run([sys.executable, "-m", "curiousj_tools.sccache_retry", str(self.rustc), *args],
                              capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)

    def log(self) -> str:
        path = self.dir / "cache" / "distbuild" / "sccache-retries.log"
        return path.read_text() if path.exists() else ""


class TestRetry(RetryCase):
    def test_success_passes_sccache_output_on(self):
        proc = self.run_retry('echo "from sccache"; echo "sccache warn" >&2', "--crate-name", "a", "a.rs")
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "from sccache\n", "sccache warn\n"))
        self.assertEqual(self.calls.read_text().splitlines(), [f"sccache {self.rustc} --crate-name a a.rs"])
        self.assertEqual(self.log(), "")

    def test_failure_runs_rustc_whose_output_alone_is_seen(self):
        proc = self.run_retry('echo "remote noise"; echo "proc macro panicked" >&2; exit 1',
                              "--crate-name", "wayland_server", "lib.rs")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "rustc --crate-name wayland_server lib.rs\n")
        self.assertEqual(proc.stderr, "rustc err\n")
        self.assertIn("wayland_server: sccache exit 1: proc macro panicked", self.log())

    def test_a_real_error_fails_after_the_local_retry(self):
        proc = self.run_retry("exit 1", "--crate-name", "a", "bad")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)

    def test_stdin_input_goes_to_rustc_directly(self):
        proc = self.run_retry("exit 1", "-", "--print", "cfg")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.calls.read_text().splitlines(), [f"rustc - --print cfg"])


if __name__ == "__main__":
    unittest.main()
