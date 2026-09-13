import io
import os
import tempfile
import unittest
from unittest import mock

from curiousj_tools import hosts, pick

SELF = {"localhost", "127.0.0.1", "::1", "my-mac", "my-mac.local"}


class DropSelf(unittest.TestCase):
    def test_drops_this_machine_in_any_spelling(self):
        entries = ["alice@my-mac.local", "MY-MAC", "localhost", "127.0.0.1", "alice@devbox"]
        self.assertEqual(hosts.drop_self(entries, SELF, "alice"), ["alice@devbox"])

    def test_keeps_another_user_on_this_machine(self):
        self.assertEqual(hosts.drop_self(["bob@my-mac"], SELF, "alice"), ["bob@my-mac"])

    def test_first_label_only_when_domain_differs(self):
        self.assertEqual(hosts.drop_self(["my-mac.example.com"], SELF, "alice"), [])
        self.assertEqual(hosts.drop_self(["my-mac2"], SELF, "alice"), ["my-mac2"])


class FindConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"HOME": self.tmp.name}

    def write(self, rel, text="h1\n"):
        path = os.path.join(self.tmp.name, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_explicit_wins(self):
        p = self.write("explicit")
        self.write(".config/ssh-hosts")
        self.assertEqual(hosts.find_config(p, "SSH_HOSTS", "ssh-hosts", self.env), p)

    def test_explicit_unreadable_is_an_error(self):
        with self.assertRaises(hosts.HostsError):
            hosts.find_config(os.path.join(self.tmp.name, "none"), "SSH_HOSTS", "ssh-hosts", self.env)

    def test_env_then_xdg(self):
        cfg = self.write(".config/ssh-hosts")
        self.assertEqual(hosts.find_config(None, "SSH_HOSTS", "ssh-hosts", self.env), cfg)
        via_env = self.write("via-env")
        env = dict(self.env, SSH_HOSTS=via_env)
        self.assertEqual(hosts.find_config(None, "SSH_HOSTS", "ssh-hosts", env), via_env)

    def test_lists_dir_between_file_var_and_xdg(self):
        xdg = self.write(".config/ssh-hosts")
        in_dir = self.write("lists/ssh-hosts")
        env = dict(self.env, SSH_LISTS_DIR=os.path.join(self.tmp.name, "lists"))
        self.assertEqual(hosts.find_config(None, "SSH_HOSTS", "ssh-hosts", env), in_dir)
        via_env = self.write("via-env")
        env["SSH_HOSTS"] = via_env
        self.assertEqual(hosts.find_config(None, "SSH_HOSTS", "ssh-hosts", env), via_env)
        env = dict(self.env, SSH_LISTS_DIR=os.path.join(self.tmp.name, "nowhere"))
        self.assertEqual(hosts.find_config(None, "SSH_HOSTS", "ssh-hosts", env), xdg)

    def test_xdg_config_home_honoured(self):
        cfg = self.write("xdg/ssh-hosts")
        env = dict(self.env, XDG_CONFIG_HOME=os.path.join(self.tmp.name, "xdg"))
        self.assertEqual(hosts.find_config(None, "SSH_HOSTS", "ssh-hosts", env), cfg)

    def test_nothing_found(self):
        with self.assertRaises(hosts.HostsError) as cm:
            hosts.find_config(None, "SSH_HOSTS", "ssh-hosts", self.env)
        self.assertIn("SSH_HOSTS", str(cm.exception))
        self.assertIn("SSH_LISTS_DIR", str(cm.exception))

    def test_read_list_skips_blanks_and_comments(self):
        p = self.write("l", "# comment\n\n  a@b  \n\nc\n")
        self.assertEqual(hosts.read_list(p), ["a@b", "c"])


class Hosts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = os.path.join(self.tmp.name, "hosts")
        with open(self.file, "w") as f:
            f.write("a\n\nalice@my-mac.local\nb\nc\n")
        patches = [mock.patch.object(hosts, "self_names", return_value=SELF),
                   mock.patch("getpass.getuser", return_value="alice")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_default_adds_localhost_last(self):
        self.assertEqual(hosts.hosts(hosts.HostOpts(file=self.file)), ["a", "b", "c", "localhost"])

    def test_no_local(self):
        self.assertEqual(hosts.hosts(hosts.HostOpts(no_local=True, file=self.file)), ["a", "b", "c"])

    def test_only_self_and_no_local_is_an_error(self):
        with open(self.file, "w") as f:
            f.write("my-mac\n")
        with self.assertRaises(hosts.HostsError):
            hosts.hosts(hosts.HostOpts(no_local=True, file=self.file))
        self.assertEqual(hosts.hosts(hosts.HostOpts(file=self.file)), ["localhost"])

    def test_pick_goes_through_picker(self):
        with mock.patch.object(pick, "pick", return_value=["c", "localhost"]) as p:
            self.assertEqual(hosts.hosts(hosts.HostOpts(pick=True, file=self.file)), ["c", "localhost"])
        p.assert_called_once_with(["a", "b", "c", "localhost"], "hosts")

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
