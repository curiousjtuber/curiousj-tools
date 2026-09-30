"""ec-magit: the elisp it builds, and that the TRAMP prefix follows the route."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from curiousj_tools import ec_magit

PREFIX = "/ssh:me@devbox:"
SOCKET = "/tmp/fwd.sock"


class TestElisp(unittest.TestCase):
    def test_escapes_backslash_then_quote(self):
        self.assertEqual(ec_magit.elisp_string('a"b\\c'), '"a\\"b\\\\c"')

    def test_no_frame(self):
        self.assertEqual(ec_magit.elisp_status("/r/", frame=False),
                         '(let ((non-essential nil) (default-directory "/r/"))'
                         ' (magit-status-setup-buffer default-directory) t)')

    def test_frame_selected_before_magit(self):
        form = ec_magit.elisp_status("/r/", frame=True)
        self.assertIn("(non-essential nil)", form)
        self.assertLess(form.index("select-frame-set-input-focus"),
                        form.index("magit-status-setup-buffer"))
        self.assertIn('(default-directory "/r/")', form)


class TestMain(unittest.TestCase):
    def setUp(self):
        self.repo = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(os.rmdir, self.repo)

    def run_main(self, argv, env, live, returncodes=(0, 0, 0)):
        calls = []
        codes = iter(returncodes)

        def fake_run(cmd, env=None, **kwargs):
            calls.append((cmd, env))
            return subprocess.CompletedProcess(cmd, next(codes))

        err = io.StringIO()
        base = {"EMACSCLIENT_BIN": "/opt/emacsclient", "EMACSCLIENT_FORWARD_SOCKET": SOCKET}
        with mock.patch.dict(os.environ, dict(base, **env), clear=True), \
             mock.patch.object(ec_magit.eca, "socket_live", return_value=live), \
             mock.patch.object(ec_magit.subprocess, "run", fake_run), \
             contextlib.redirect_stderr(err):
            rc = ec_magit.main(argv)
        return rc, calls, err.getvalue()

    def test_live_forward_gets_prefix_and_socket(self):
        rc, calls, _ = self.run_main(["-n", self.repo], {"EMACSCLIENT_TRAMP_PREFIX": PREFIX}, True)
        self.assertEqual(rc, 0)
        (cmd, env), = calls
        self.assertEqual(cmd[:2], ["/opt/emacsclient", "-e"])
        self.assertIn(f'"{PREFIX}{self.repo}/"', cmd[2])
        self.assertEqual(env["EMACS_SOCKET_NAME"], SOCKET)
        self.assertNotIn("EMACSCLIENT_TRAMP", env)

    def test_dead_forward_gets_plain_path(self):
        # The bug this command exists for: prefix set, forward gone.
        rc, calls, _ = self.run_main(["-n", self.repo], {"EMACSCLIENT_TRAMP_PREFIX": PREFIX}, False)
        self.assertEqual(rc, 0)
        (cmd, env), = calls
        self.assertIn(f'"{self.repo}/"', cmd[2])
        self.assertNotIn(PREFIX, cmd[2])
        self.assertNotIn("EMACS_SOCKET_NAME", env)

    def test_no_prefix_is_local_without_probing(self):
        with mock.patch.object(ec_magit.eca, "socket_live") as probe:
            with mock.patch.dict(os.environ, {"EMACSCLIENT_BIN": "/opt/emacsclient"}, clear=True), \
                 mock.patch.object(ec_magit.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 0)) as run:
                self.assertEqual(ec_magit.main(["-n", self.repo]), 0)
        probe.assert_not_called()
        self.assertIn(f'"{self.repo}/"', run.call_args.args[0][2])

    def test_one_call_per_dir_default_frame(self):
        rc, calls, _ = self.run_main([self.repo, self.repo], {}, False)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("make-frame", calls[0][0][2])

    def test_default_is_cwd(self):
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.repo)
        rc, calls, _ = self.run_main(["-n"], {}, False)
        self.assertEqual(rc, 0)
        self.assertIn(f'"{self.repo}/"', calls[0][0][2])

    def test_missing_dir_opens_nothing(self):
        rc, calls, err = self.run_main([self.repo, self.repo + "/nope"], {}, False)
        self.assertEqual(rc, 1)
        self.assertEqual(calls, [])
        self.assertIn("no such directory", err)

    def test_failure_stops_the_run(self):
        rc, calls, err = self.run_main([self.repo, self.repo], {}, False, returncodes=[1, 0])
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn(f"failed to open {self.repo}", err)


if __name__ == "__main__":
    unittest.main()
