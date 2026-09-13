import io
import unittest
from unittest import mock

from curiousj_tools import hosts, pick, xssh


class SplitArgs(unittest.TestCase):
    def test_host_flags_anywhere_rest_passes_through_in_order(self):
        opts, rest = xssh.split_args(["--stay", "-N", "-l", "ev", "-f", "F", "-p", "-s"])
        self.assertEqual(opts, hosts.HostOpts(True, True, "F"))
        self.assertEqual(rest, ["--stay", "-l", "ev", "-s"])


class PaneCommands(unittest.TestCase):
    def test_localhost_becomes_a_local_shell(self):
        self.assertEqual(xssh.pane_commands(["a", "b@c", "localhost"]),
                         ["ssh a", "ssh b@c", "cd ~; exec $SHELL"])


class Main(unittest.TestCase):
    def test_execs_xpanes(self):
        with mock.patch.object(hosts, "hosts", return_value=["a", "localhost"]) as h, \
                mock.patch("shutil.which", return_value="/usr/bin/xpanes"), \
                mock.patch("os.execvp") as ex, \
                mock.patch.object(hosts, "confirm_new_hosts"):
            xssh.main(["--stay", "-N", "-l", "ev"])
        h.assert_called_once_with(hosts.HostOpts(no_local=True))
        ex.assert_called_once_with(
            "xpanes", ["xpanes", "--stay", "-l", "ev", "-e", "ssh a", "cd ~; exec $SHELL"])

    def test_missing_xpanes_and_abort(self):
        with mock.patch("shutil.which", return_value=None), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(xssh.main([]), 1)
        self.assertIn("xpanes not installed", err.getvalue())
        with mock.patch("shutil.which", return_value="/usr/bin/xpanes"), \
                mock.patch.object(hosts, "hosts", side_effect=pick.Abort), \
                mock.patch("os.execvp") as ex, \
                mock.patch.object(hosts, "confirm_new_hosts"):
            self.assertEqual(xssh.main(["-p"]), 130)
        ex.assert_not_called()

    def test_help(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(xssh.main(["-h"]), 0)
        self.assertIn("xssh [-h]", out.getvalue())


if __name__ == "__main__":
    unittest.main()
