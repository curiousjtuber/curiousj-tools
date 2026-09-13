"""ec-browse: the elisp it builds and which emacsclient it calls."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import unittest
from unittest import mock

from curiousj_tools import ec_browse


class TestElisp(unittest.TestCase):
    def test_plain_url(self):
        self.assertEqual(ec_browse.elisp_call("browse-url", "https://example.com/a?b=1"),
                         '(browse-url "https://example.com/a?b=1")')

    def test_escapes_backslash_then_quote(self):
        self.assertEqual(ec_browse.elisp_call("f", 'x"y\\z'), '(f "x\\"y\\\\z")')


class TestEmacsclientCommand(unittest.TestCase):
    def test_env_override(self):
        self.assertEqual(ec_browse.emacsclient_command(
            {"EC_BROWSE_EMACSCLIENT": "/opt/ec", "PATH": "/nowhere"}), ["/opt/ec"])

    def test_wrapper_on_path(self):
        with mock.patch.object(ec_browse.shutil, "which", return_value="/usr/local/bin/emacsclient-auto"):
            self.assertEqual(ec_browse.emacsclient_command({"PATH": "/x"}),
                             ["/usr/local/bin/emacsclient-auto"])

    def test_module_fallback(self):
        with mock.patch.object(ec_browse.shutil, "which", return_value=None):
            self.assertEqual(ec_browse.emacsclient_command({"PATH": "/x"}),
                             [sys.executable, "-m", "curiousj_tools.emacsclient_auto"])


class TestMain(unittest.TestCase):
    def run_main(self, argv, env, returncodes):
        calls = []
        codes = iter(returncodes)

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, next(codes))

        err = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(ec_browse.subprocess, "run", fake_run), \
             contextlib.redirect_stderr(err):
            rc = ec_browse.main(argv)
        return rc, calls, err.getvalue()

    def test_no_urls_is_usage(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(ec_browse.main([]), 2)
        self.assertIn("usage: ec-browse", err.getvalue())

    def test_one_call_per_url(self):
        env = {"EC_BROWSE_EMACSCLIENT": "/opt/ec"}
        rc, calls, err = self.run_main(["https://a.example", "https://b.example"], env, [0, 0])
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [
            ["/opt/ec", "-e", '(browse-url "https://a.example")'],
            ["/opt/ec", "-e", '(browse-url "https://b.example")'],
        ])
        self.assertEqual(err, "")

    def test_function_override_and_failure_status(self):
        env = {"EC_BROWSE_EMACSCLIENT": "/opt/ec",
               "EC_BROWSE_FUNCTION": "browse-url-default-browser"}
        rc, calls, err = self.run_main(["https://a.example", "https://b.example"], env, [1, 0])
        self.assertEqual(rc, 1)
        self.assertEqual(calls[0][2], '(browse-url-default-browser "https://a.example")')
        self.assertIn("failed to open https://a.example", err)
        self.assertNotIn("b.example", err)


if __name__ == "__main__":
    unittest.main()
