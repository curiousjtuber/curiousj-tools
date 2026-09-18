import io
import os
import tempfile
import unittest
from unittest import mock

from curiousj_tools import logins, pick
from curiousj_tools.lists import Login

SELF = {"localhost", "127.0.0.1", "::1", "my-mac", "my-mac.local"}


def H(*names):
    return [Login(n) for n in names]


class DropSelf(unittest.TestCase):
    def test_drops_this_machine_in_any_spelling(self):
        entries = H("alice@my-mac.local", "MY-MAC", "localhost", "127.0.0.1", "alice@devbox")
        self.assertEqual(logins.drop_self(entries, SELF, "alice"), H("alice@devbox"))

    def test_keeps_another_user_on_this_machine(self):
        self.assertEqual(logins.drop_self(H("bob@my-mac"), SELF, "alice"), H("bob@my-mac"))

    def test_first_label_only_when_domain_differs(self):
        self.assertEqual(logins.drop_self(H("my-mac.example.com"), SELF, "alice"), [])
        self.assertEqual(logins.drop_self(H("my-mac2"), SELF, "alice"), H("my-mac2"))


class Hosts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = os.path.join(self.tmp.name, "ssh-lists.toml")
        with open(self.file, "w") as f:
            f.write('logins = ["a", "alice@my-mac.local", "b", "c"]\n')
        patches = [mock.patch.object(logins, "self_names", return_value=SELF),
                   mock.patch("getpass.getuser", return_value="alice")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_default_adds_localhost_last(self):
        self.assertEqual(logins.logins(logins.LoginOpts(file=self.file)), H("a", "b", "c", "localhost"))

    def test_no_local(self):
        self.assertEqual(logins.logins(logins.LoginOpts(no_local=True, file=self.file)), H("a", "b", "c"))

    def test_only_self_and_no_local_is_an_error(self):
        with open(self.file, "w") as f:
            f.write('logins = ["my-mac"]\n')
        with self.assertRaises(logins.ToolError):
            logins.logins(logins.LoginOpts(no_local=True, file=self.file))
        self.assertEqual(logins.logins(logins.LoginOpts(file=self.file)), H("localhost"))

    def test_pick_goes_through_picker(self):
        with mock.patch.object(pick, "pick", return_value=["c", "localhost"]) as p:
            self.assertEqual(logins.logins(logins.LoginOpts(pick=True, file=self.file)),
                             H("c", "localhost"))
        p.assert_called_once_with(["a", "b", "c", "localhost"], "logins")

    def test_pick_tells_twins_apart(self):
        # The same login twice, once with commands: picking one is one.
        with open(self.file, "w") as f:
            f.write('logins = ["a", { login = "a", commands = ["exec zsh"] }]\n')
        with mock.patch.object(pick, "pick", return_value=["a  exec zsh"]) as p:
            self.assertEqual(logins.logins(logins.LoginOpts(pick=True, file=self.file)),
                             [Login("a", ["exec zsh"])])
        p.assert_called_once_with(["a", "a  exec zsh", "localhost"], "logins")

    def test_commands_ride_along(self):
        with open(self.file, "w") as f:
            f.write('[[logins]]\nlogin = "a"\ncommands = ["exec zsh"]\n')
        self.assertEqual(logins.logins(logins.LoginOpts(no_local=True, file=self.file)),
                         [Login("a", ["exec zsh"])])

    def test_main_prints_and_reports(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(logins.main(["-f", self.file]), 0)
        self.assertEqual(out.getvalue(), "a\nb\nc\nlocalhost\n")
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(logins.main(["-f", os.path.join(self.tmp.name, "none")]), 1)
        self.assertIn("cannot read", err.getvalue())
        with mock.patch.object(pick, "pick", side_effect=pick.Abort):
            self.assertEqual(logins.main(["-p", "-f", self.file]), 130)


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = os.path.join(self.tmp.name, "ssh-lists.toml")
        with open(self.file, "w") as f:
            f.write('logins = ["a", "b"]\n')
        p = mock.patch.object(logins, "self_names", return_value=SELF)
        p.start()
        self.addCleanup(p.stop)

    def test_short_flags_combine(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(logins.main(["-Nf", self.file]), 0)
        self.assertEqual(out.getvalue(), "a\nb\n")

    def test_help_is_the_docstring_then_the_options(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(logins.main(["-h"]), 0)
        text = out.getvalue()
        self.assertTrue(text.startswith("ssh-logins -- print the login list"))
        self.assertIn("\n    ssh-logins [-h] [-N|--no-local]", text)
        self.assertIn("\nOptions:\n", text)
        self.assertIn("-N, --no-local", text)

    def test_bad_option_is_a_usage_error(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(logins.main(["-x"]), 2)
        self.assertIn("No such option '-x'", err.getvalue())
        self.assertIn("ssh-logins --help", err.getvalue())


if __name__ == "__main__":
    unittest.main()
