"""emacsclient-auto: routing decisions and the PATH scan, without an Emacs."""

from __future__ import annotations

import contextlib
import io
import os
import stat
import tempfile
import unittest
from unittest import mock

from curiousj_tools import emacsclient_auto as eca

PREFIX = "/ssh:user@devbox:"


def make_exe(path: str, body: str = "#!/bin/sh\nexit 0\n") -> str:
    with open(path, "w") as fh:
        fh.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


class TestDefaultSocket(unittest.TestCase):
    def test_named_after_the_login(self):
        with mock.patch.object(eca.getpass, "getuser", return_value="root"):
            self.assertEqual(eca.default_socket(), "/tmp/emacs-remote-socket-root")

    def test_uid_when_the_login_cannot_be_told(self):
        with mock.patch.object(eca.getpass, "getuser", side_effect=KeyError), \
             mock.patch.object(eca.os, "getuid", return_value=1000):
            self.assertEqual(eca.default_socket(), "/tmp/emacs-remote-socket-1000")


class TestRoute(unittest.TestCase):
    def test_settings_default_and_knobs(self):
        self.assertEqual(eca.settings({}), (eca.DEFAULT_SOCKET, ""))
        self.assertEqual(eca.settings({"EMACSCLIENT_TRAMP_PREFIX": PREFIX,
                                       "EMACSCLIENT_FORWARD_SOCKET": "/run/fwd.sock"}),
                         ("/run/fwd.sock", PREFIX))

    def test_live_socket_with_prefix_routes_files_remote(self):
        branch, extra = eca.route(["-n", "notes.txt"], "/run/fwd.sock", PREFIX, live=True)
        self.assertEqual(branch, "remote")
        self.assertEqual(extra, {"EMACS_SOCKET_NAME": "/run/fwd.sock",
                                 "EMACSCLIENT_TRAMP": PREFIX})

    def test_eval_goes_remote_without_prefix(self):
        for args in (["-e", "(magit-status)"], ["--eval", "t"], ["--eval=t"]):
            branch, extra = eca.route(args, eca.DEFAULT_SOCKET, PREFIX, live=True)
            self.assertEqual(branch, "remote-eval", args)
            self.assertEqual(extra, {"EMACS_SOCKET_NAME": eca.DEFAULT_SOCKET})

    def test_dead_socket_is_local(self):
        branch, extra = eca.route(["f"], eca.DEFAULT_SOCKET, PREFIX, live=False)
        self.assertEqual((branch, extra), ("local", {}))

    def test_no_prefix_is_local_even_when_live(self):
        branch, extra = eca.route(["f"], eca.DEFAULT_SOCKET, "", live=True)
        self.assertEqual((branch, extra), ("local", {}))

    def test_is_eval(self):
        self.assertTrue(eca.is_eval(["-e", "t"]))
        self.assertTrue(eca.is_eval(["--eval=t"]))
        self.assertFalse(eca.is_eval(["-n", "-e.txt", "--evaluate"]))


class TestFindEmacsclient(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wrapper_dir = os.path.join(self.tmp.name, "wrap")
        self.real_dir = os.path.join(self.tmp.name, "real")
        os.makedirs(self.wrapper_dir)
        os.makedirs(self.real_dir)
        # An installed wrapper, plus an `emacsclient` symlink pointing at it,
        # the way a user shadows the real client with the router.
        self.wrapper = make_exe(os.path.join(self.wrapper_dir, "emacsclient-auto"))
        os.symlink(self.wrapper, os.path.join(self.wrapper_dir, "emacsclient"))
        self.real = make_exe(os.path.join(self.real_dir, "emacsclient"))
        self.path = os.pathsep.join([self.wrapper_dir, self.real_dir])

    def test_skips_symlink_to_itself(self):
        found = eca.find_emacsclient({"PATH": self.path}, self={self.wrapper})
        self.assertEqual(found, self.real)

    def test_skips_by_name_when_self_paths_are_unknown(self):
        found = eca.find_emacsclient({"PATH": self.path}, self=set())
        self.assertEqual(found, self.real)

    def test_explicit_bin_wins(self):
        env = {"PATH": self.path, "EMACSCLIENT_BIN": "/opt/emacs/bin/emacsclient"}
        self.assertEqual(eca.find_emacsclient(env, self=set()), "/opt/emacs/bin/emacsclient")

    def test_nothing_found(self):
        self.assertIsNone(eca.find_emacsclient({"PATH": self.wrapper_dir}, self=set()))
        self.assertIsNone(eca.find_emacsclient({"PATH": ""}, self=set()))

    def test_non_executable_is_skipped(self):
        os.chmod(self.real, 0o644)
        self.assertIsNone(eca.find_emacsclient({"PATH": self.path}, self=set()))


class TestMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.real = make_exe(os.path.join(self.tmp.name, "emacsclient"))
        self.base_env = {"PATH": self.tmp.name}

    def run_main(self, args, env, live):
        calls = []

        def fake_exec(file, argv, exec_env):
            calls.append((file, argv, exec_env))

        err = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(eca, "socket_live", return_value=live) as probe, \
             mock.patch.object(os, "execvpe", fake_exec), \
             contextlib.redirect_stderr(err):
            rc = eca.main(args)
        return rc, calls, probe, err.getvalue()

    def test_remote_exec_sets_socket_and_tramp(self):
        env = dict(self.base_env, EMACSCLIENT_TRAMP_PREFIX=PREFIX, EMACSCLIENT_AUTO_DEBUG="1")
        rc, calls, probe, err = self.run_main(["-n", "a.txt"], env, live=True)
        self.assertEqual(rc, 0)
        probe.assert_called_once()
        (file, argv, exec_env), = calls
        self.assertEqual(file, self.real)
        self.assertEqual(argv, [self.real, "-n", "a.txt"])
        self.assertEqual(exec_env["EMACS_SOCKET_NAME"], eca.DEFAULT_SOCKET)
        self.assertEqual(exec_env["EMACSCLIENT_TRAMP"], PREFIX)
        self.assertIn("remote via", err)

    def test_local_exec_when_socket_dead(self):
        env = dict(self.base_env, EMACSCLIENT_TRAMP_PREFIX=PREFIX, EMACSCLIENT_AUTO_DEBUG="1")
        rc, calls, _, err = self.run_main(["a.txt"], env, live=False)
        self.assertEqual(rc, 0)
        (_, argv, exec_env), = calls
        self.assertEqual(argv, [self.real, "a.txt"])
        self.assertNotIn("EMACS_SOCKET_NAME", exec_env)
        self.assertNotIn("EMACSCLIENT_TRAMP", exec_env)
        self.assertIn("local (", err)

    def test_probe_skipped_without_prefix(self):
        rc, calls, probe, _ = self.run_main(["a.txt"], dict(self.base_env), live=True)
        self.assertEqual(rc, 0)
        probe.assert_not_called()
        self.assertNotIn("EMACS_SOCKET_NAME", calls[0][2])

    def test_explicit_bin_is_execd(self):
        env = dict(self.base_env, EMACSCLIENT_BIN="/opt/emacsclient")
        rc, calls, _, _ = self.run_main(["x"], env, live=False)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][0], "/opt/emacsclient")

    def test_missing_emacsclient_is_127(self):
        empty = tempfile.mkdtemp(dir=self.tmp.name)
        rc, calls, _, err = self.run_main(["x"], {"PATH": empty}, live=False)
        self.assertEqual(rc, 127)
        self.assertEqual(calls, [])
        self.assertIn("could not find the real emacsclient", err)


class TestSocketLive(unittest.TestCase):
    def test_missing_or_plain_file_is_not_live(self):
        with tempfile.TemporaryDirectory() as d:
            plain = os.path.join(d, "sock")
            open(plain, "w").close()
            self.assertFalse(eca.socket_live(plain, "/bin/true", {}))
            self.assertFalse(eca.socket_live(os.path.join(d, "none"), "/bin/true", {}))


if __name__ == "__main__":
    unittest.main()
