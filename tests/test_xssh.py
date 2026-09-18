import io
import subprocess
import unittest
from unittest import mock

from curiousj_tools import logins, pick, xssh
from curiousj_tools.lists import Login


class Arguments(unittest.TestCase):
    def test_host_flags_anywhere_rest_passes_through_in_order(self):
        with mock.patch.object(logins, "logins", return_value=[Login("a")]) as h, \
                mock.patch("shutil.which", return_value="/usr/bin/xpanes"), \
                mock.patch("os.execvp") as ex, \
                mock.patch.object(logins, "confirm_new_hosts"):
            xssh.main(["--stay", "-N", "-l", "ev", "-f", "F", "-p", "-s"])
        h.assert_called_once_with(logins.LoginOpts(True, True, "F"))
        self.assertEqual(ex.call_args[0][1], ["xpanes", "--stay", "-l", "ev", "-s", "-e", "ssh a"])


class PaneCommands(unittest.TestCase):
    def test_localhost_becomes_a_local_shell(self):
        self.assertEqual(xssh.pane_commands([Login("a"), Login("b@c"), Login("localhost")]),
                         ["ssh a", "ssh b@c", "cd ~; exec $SHELL"])

    def test_via_is_not_the_panes_business(self):
        self.assertEqual(xssh.pane_commands([Login("a", via="distrobox enter dev --")]), ["ssh a"])

    def test_commands_run_after_login_on_a_terminal(self):
        self.assertEqual(xssh.pane_commands([Login("a", ["distrobox enter dev"]),
                                             Login("b", ["cd src", "exec zsh"])]),
                         ["ssh -t a 'distrobox enter dev'", "ssh -t b 'cd src; exec zsh'"])

    def test_ampersands_survive_xpanes_substitution(self):
        # xpanes types "${_cmd//{}/arg}" into the pane; feed the escaped
        # command through that very expansion in a bash that expands `&`.
        cmd = xssh.pane_command(Login("a", ["cd src && ls a\\b", "exec zsh"]))
        escaped = xssh.for_xpanes(cmd, ampersand=True)
        self.assertEqual(xssh.for_xpanes(cmd, ampersand=False), cmd)
        proc = subprocess.run(
            ["bash", "-c", 'shopt -q patsub_replacement || exit 3; _cmd="{}"; '
             'printf %s "${_cmd//\\{\\}/${1}}"', "_", escaped],
            capture_output=True, text=True)
        if proc.returncode == 3:
            self.skipTest("bash without patsub_replacement")
        self.assertEqual(proc.stdout, cmd)


class Main(unittest.TestCase):
    def test_execs_xpanes(self):
        with mock.patch.object(logins, "logins", return_value=[Login("a"), Login("localhost")]) as h, \
                mock.patch("shutil.which", return_value="/usr/bin/xpanes"), \
                mock.patch("os.execvp") as ex, \
                mock.patch.object(logins, "confirm_new_hosts") as confirm:
            xssh.main(["--stay", "-N", "-l", "ev"])
        h.assert_called_once_with(logins.LoginOpts(no_local=True))
        confirm.assert_called_once_with(["a", "localhost"], "xssh")
        ex.assert_called_once_with(
            "xpanes", ["xpanes", "--stay", "-l", "ev", "-e", "ssh a", "cd ~; exec $SHELL"])

    def test_escapes_only_when_a_host_has_commands(self):
        entries = [Login("a", ["cd x && exec zsh"]), Login("localhost")]
        with mock.patch.object(logins, "logins", return_value=entries), \
                mock.patch("shutil.which", return_value="/usr/bin/xpanes"), \
                mock.patch("os.execvp") as ex, \
                mock.patch.object(xssh, "xpanes_expands_ampersand", return_value=True) as bash, \
                mock.patch.object(logins, "confirm_new_hosts"):
            xssh.main([])
        bash.assert_called_once()
        self.assertEqual(ex.call_args[0][1][-2:], ["ssh -t a 'cd x \\&\\& exec zsh'", "cd ~; exec $SHELL"])
        with mock.patch.object(logins, "logins", return_value=[Login("a")]), \
                mock.patch("shutil.which", return_value="/usr/bin/xpanes"), \
                mock.patch("os.execvp"), \
                mock.patch.object(xssh, "xpanes_expands_ampersand") as bash, \
                mock.patch.object(logins, "confirm_new_hosts"):
            xssh.main([])
        bash.assert_not_called()

    def test_missing_xpanes_and_abort(self):
        with mock.patch("shutil.which", return_value=None), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(xssh.main([]), 1)
        self.assertIn("xpanes not installed", err.getvalue())
        with mock.patch("shutil.which", return_value="/usr/bin/xpanes"), \
                mock.patch.object(logins, "logins", side_effect=pick.Abort), \
                mock.patch("os.execvp") as ex, \
                mock.patch.object(logins, "confirm_new_hosts"):
            self.assertEqual(xssh.main(["-p"]), 130)
        ex.assert_not_called()

    def test_help(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(xssh.main(["-h"]), 0)
        self.assertIn("xssh [-h]", out.getvalue())


if __name__ == "__main__":
    unittest.main()
