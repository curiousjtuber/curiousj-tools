import io
import os
import tempfile
import unittest
from unittest import mock

from curiousj_tools import attrs, lists, logins, pick
from curiousj_tools.attrs import Term
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

    def test_local_login_inherits_the_self_entrys_attributes_and_operations(self):
        inside = Login("my-mac", via="distrobox enter dev --", attributes={"dbx": None})
        me = Login("alice@my-mac.local", ["exec zsh"], attributes={"mac": None, "brew": None},
                   operations={"sys": "brew upgrade"}, file="F")
        entries = [Login("a"), inside, me, Login("my-mac", attributes={"other": None})]
        local = logins.local_login(entries, SELF, "alice")
        self.assertEqual(local, Login("localhost", attributes={"mac": None, "brew": None},
                                      operations={"sys": "brew upgrade"}))
        self.assertEqual((local.commands, local.via, local.file), ([], None, "F"))
        # a via entry is a place inside the machine, not the machine; another user is not me
        self.assertEqual(logins.local_login([inside, Login("bob@my-mac")], SELF, "alice"),
                         Login("localhost"))


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

    def test_a_user_without_a_passwd_entry_is_its_uid(self):
        # getuser() raising used to take the whole list down with it. The
        # uid names no entry's user@, so alice@my-mac.local is someone else.
        with mock.patch("getpass.getuser", side_effect=KeyError("uid 1000")), \
                mock.patch("os.getuid", return_value=1000):
            got = logins.logins(logins.LoginOpts(files=(self.file,)))
        self.assertEqual([e.login for e in got], ["a", "alice@my-mac.local", "b", "c", "localhost"])

    def test_default_adds_localhost_last(self):
        self.assertEqual(logins.logins(logins.LoginOpts(files=(self.file,))), H("a", "b", "c", "localhost"))

    def test_no_local(self):
        self.assertEqual(logins.logins(logins.LoginOpts(no_local=True, files=(self.file,))), H("a", "b", "c"))

    def test_only_self_and_no_local_is_an_error(self):
        with open(self.file, "w") as f:
            f.write('logins = ["my-mac"]\n')
        with self.assertRaises(logins.ToolError):
            logins.logins(logins.LoginOpts(no_local=True, files=(self.file,)))
        self.assertEqual(logins.logins(logins.LoginOpts(files=(self.file,))), H("localhost"))

    def test_pick_goes_through_picker(self):
        with mock.patch.object(pick, "pick", return_value=["c", "localhost"]) as p:
            self.assertEqual(logins.logins(logins.LoginOpts(pick=True, files=(self.file,))),
                             H("c", "localhost"))
        p.assert_called_once_with(["a", "b", "c", "localhost"], "logins")

    def test_pick_tells_twins_apart(self):
        # The same login twice, once with commands: picking one is one.
        with open(self.file, "w") as f:
            f.write('logins = ["a", { login = "a", commands = ["exec zsh"] }]\n')
        with mock.patch.object(pick, "pick", return_value=["a  exec zsh"]) as p:
            self.assertEqual(logins.logins(logins.LoginOpts(pick=True, files=(self.file,))),
                             [Login("a", ["exec zsh"])])
        p.assert_called_once_with(["a", "a  exec zsh", "localhost"], "logins")

    def test_commands_ride_along(self):
        with open(self.file, "w") as f:
            f.write('[[logins]]\nlogin = "a"\ncommands = ["exec zsh"]\n')
        self.assertEqual(logins.logins(logins.LoginOpts(no_local=True, files=(self.file,))),
                         [Login("a", ["exec zsh"])])

    def test_attr_terms_filter_logins_and_localhost_alike(self):
        with open(self.file, "w") as f:
            f.write('logins = [{ login = "a", attributes = ["mise"] }, "b",\n'
                    '  { login = "alice@my-mac.local", attributes = ["mise", "mac"] }]\n')
        opts = logins.LoginOpts(files=(self.file,), attrs=(Term("mise"),))
        found = logins.logins(opts)
        self.assertEqual([e.login for e in found], ["a", "localhost"])
        self.assertEqual(found[1].attributes, {"mise": None, "mac": None})
        opts = logins.LoginOpts(files=(self.file,), attrs=(Term("mise", negated=True),))
        self.assertEqual([e.login for e in logins.logins(opts)], ["b"])
        opts = logins.LoginOpts(files=(self.file,), attrs=(Term("mac"), Term("mise")))
        self.assertEqual([e.login for e in logins.logins(opts)], ["localhost"])

    def test_nothing_matching_is_an_error(self):
        with self.assertRaises(logins.ToolError) as cm:
            logins.logins(logins.LoginOpts(files=(self.file,), attrs=(Term("mac"),)))
        self.assertIn(f"no logins in {self.file} match -a mac", str(cm.exception))
        with self.assertRaises(logins.ToolError) as cm:
            logins.logins(logins.LoginOpts(no_local=True, files=(self.file,), attrs=(Term("x"),)))
        self.assertIn("match -a x", str(cm.exception))

    def test_a_second_file_adds_logins_and_found_lists_are_taken_as_given(self):
        second = os.path.join(self.tmp.name, "ssh-lists-more.toml")
        with open(second, "w") as f:
            f.write('logins = ["d"]\n')
        self.assertEqual([e.login for e in logins.logins(logins.LoginOpts(files=(self.file, second)))],
                         ["a", "b", "c", "d", "localhost"])
        found = lists.Lists([Login("z")], files=["Z"])
        with mock.patch.object(lists, "load_all") as load:
            self.assertEqual([e.login for e in logins.logins(logins.LoginOpts(), found=found)],
                             ["z", "localhost"])
        load.assert_not_called()

    def test_main_prints_and_reports(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(logins.main(["-f", self.file]), 0)
        self.assertEqual(out.getvalue(), "a\nb\nc\nlocalhost\n")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(logins.main(["-f", self.file, "-a", "!x", "-N"]), 0)
        self.assertEqual(out.getvalue(), "a\nb\nc\n")
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

    def test_bad_attr_term_is_a_usage_error(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(logins.main(["-a", "=v", "-f", self.file]), 2)
        self.assertIn("'-a' / '--attr': '=v': a key is a word", err.getvalue())

    def test_help_lists_attr(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(logins.main(["-h"]), 0)
        self.assertIn("-a, --attr TERM", out.getvalue())
        self.assertIn("[-a TERM]", out.getvalue())


if __name__ == "__main__":
    unittest.main()
