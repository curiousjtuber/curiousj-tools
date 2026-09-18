import io
import os
import tempfile
import unittest
from unittest import mock

from curiousj_tools import hosts, pick
from curiousj_tools.lists import HostInfo

SELF = {"localhost", "127.0.0.1", "::1", "my-mac", "my-mac.local"}


def H(*names):
    return [HostInfo(n) for n in names]


class DropSelf(unittest.TestCase):
    def test_drops_this_machine_in_any_spelling(self):
        entries = H("alice@my-mac.local", "MY-MAC", "localhost", "127.0.0.1", "alice@devbox")
        self.assertEqual(hosts.drop_self(entries, SELF, "alice"), H("alice@devbox"))

    def test_keeps_another_user_on_this_machine(self):
        self.assertEqual(hosts.drop_self(H("bob@my-mac"), SELF, "alice"), H("bob@my-mac"))

    def test_first_label_only_when_domain_differs(self):
        self.assertEqual(hosts.drop_self(H("my-mac.example.com"), SELF, "alice"), [])
        self.assertEqual(hosts.drop_self(H("my-mac2"), SELF, "alice"), H("my-mac2"))


class Hosts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = os.path.join(self.tmp.name, "ssh-lists.toml")
        with open(self.file, "w") as f:
            f.write('hosts = ["a", "alice@my-mac.local", "b", "c"]\n')
        patches = [mock.patch.object(hosts, "self_names", return_value=SELF),
                   mock.patch("getpass.getuser", return_value="alice")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_default_adds_localhost_last(self):
        self.assertEqual(hosts.hosts(hosts.HostOpts(file=self.file)), H("a", "b", "c", "localhost"))

    def test_no_local(self):
        self.assertEqual(hosts.hosts(hosts.HostOpts(no_local=True, file=self.file)), H("a", "b", "c"))

    def test_only_self_and_no_local_is_an_error(self):
        with open(self.file, "w") as f:
            f.write('hosts = ["my-mac"]\n')
        with self.assertRaises(hosts.HostsError):
            hosts.hosts(hosts.HostOpts(no_local=True, file=self.file))
        self.assertEqual(hosts.hosts(hosts.HostOpts(file=self.file)), H("localhost"))

    def test_pick_goes_through_picker(self):
        with mock.patch.object(pick, "pick", return_value=["c", "localhost"]) as p:
            self.assertEqual(hosts.hosts(hosts.HostOpts(pick=True, file=self.file)),
                             H("c", "localhost"))
        p.assert_called_once_with(["a", "b", "c", "localhost"], "hosts")

    def test_commands_ride_along(self):
        with open(self.file, "w") as f:
            f.write('[[hosts]]\nhost = "a"\ncommands = ["exec zsh"]\n')
        self.assertEqual(hosts.hosts(hosts.HostOpts(no_local=True, file=self.file)),
                         [HostInfo("a", ["exec zsh"])])

    def test_main_prints_and_reports(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(hosts.main(["-f", self.file]), 0)
        self.assertEqual(out.getvalue(), "a\nb\nc\nlocalhost\n")
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(hosts.main(["-f", os.path.join(self.tmp.name, "none")]), 1)
        self.assertIn("cannot read", err.getvalue())
        with mock.patch.object(pick, "pick", side_effect=pick.Abort):
            self.assertEqual(hosts.main(["-p", "-f", self.file]), 130)


class HostOpts(unittest.TestCase):
    def test_argv_round_trip(self):
        self.assertEqual(hosts.HostOpts().argv(), [])
        self.assertEqual(hosts.HostOpts(True, True, "F").argv(), ["-N", "-p", "-f", "F"])

    def test_take_host_opt(self):
        opts = hosts.HostOpts()
        argv = ["-N", "-f", "F", "--pick", "x"]
        self.assertEqual(hosts.take_host_opt(argv, 0, opts, "t"), 1)
        self.assertEqual(hosts.take_host_opt(argv, 1, opts, "t"), 3)
        self.assertEqual(hosts.take_host_opt(argv, 3, opts, "t"), 4)
        self.assertEqual(hosts.take_host_opt(argv, 4, opts, "t"), 4)
        self.assertEqual(opts, hosts.HostOpts(True, True, "F"))
        with self.assertRaises(hosts.UsageError):
            hosts.take_host_opt(["-f"], 0, hosts.HostOpts(), "t")


if __name__ == "__main__":
    unittest.main()
