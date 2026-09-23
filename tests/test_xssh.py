import io
import subprocess
import unittest
from unittest import mock

import base64

from curiousj_tools import attrs, lists, logins, pick, xssh
from curiousj_tools.lists import Login, Operation, PathInfo


class Arguments(unittest.TestCase):
    def test_host_flags_anywhere_rest_passes_through_in_order(self):
        with mock.patch.object(logins, "select", return_value=[Login("a")]) as h, \
                mock.patch("shutil.which", return_value="/usr/bin/xpanes"), \
                mock.patch("os.execvp") as ex, \
                mock.patch.object(logins, "confirm_new_hosts"):
            xssh.main(["--stay", "-N", "-l", "ev", "-f", "F", "-p", "-s"])
        h.assert_called_once_with(logins.LoginOpts(True, True, ("F",)))
        self.assertEqual(ex.call_args[0][1], ["xpanes", "--stay", "-l", "ev", "-s", "-e", "ssh a"])

    def test_xpanes_c_and_C_pass_through(self):
        with mock.patch.object(logins, "select", return_value=[Login("a")]), \
                mock.patch("shutil.which", return_value="/usr/bin/xpanes"), \
                mock.patch("os.execvp") as ex, \
                mock.patch.object(logins, "confirm_new_hosts"):
            xssh.main(["-C", "2", "-c", "echo {}"])
        self.assertEqual(ex.call_args[0][1], ["xpanes", "-C", "2", "-c", "echo {}", "-e", "ssh a"])

    def test_list_ops_prints_without_xpanes(self):
        found = lists.Lists(operations={"up": Operation("up", "mise up")}, files=["F"])
        with mock.patch.object(lists, "load_all", return_value=found) as load, \
                mock.patch("shutil.which", return_value=None), \
                mock.patch("os.execvp") as ex, \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(xssh.main(["-L", "-f", "F"]), 0)
        load.assert_called_once_with(("F",))
        ex.assert_not_called()
        self.assertEqual(out.getvalue(), "up  per login  mise up\n")

    def test_path_flags_need_an_op(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(xssh.main(["--clone"]), 2)
        self.assertIn("go with -o NAME", err.getvalue())


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
        # command through that very expansion in a bash that expands '&'.
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


def decoded(pane):
    """The script a pane command decodes and runs."""
    encoded = pane.split("$(echo ", 1)[1].split(" | base64 -d)")[0]
    return base64.b64decode(encoded).decode()


class OpPanes(unittest.TestCase):
    OPS = [Operation("up", "mise up", logins=attrs.condition("mise", "T")),
           Operation("gp", "git pull", paths=attrs.condition("git", "T"))]
    PATHS = [PathInfo("p", attributes={"git": None}), PathInfo("q")]

    def test_pane_runs_the_script_through_via_then_the_logins_commands(self):
        entry = Login("a", ["distrobox enter dev"], "distrobox enter dev --", {"mise": None})
        pane = xssh.op_pane_command(entry, "rc=0\nexit $rc")
        self.assertTrue(pane.startswith("ssh -t a 'sh -c \"$(echo "), pane)
        self.assertTrue(pane.endswith(" | base64 -d)\"; distrobox enter dev'"), pane)
        self.assertNotIn("\n", pane)
        self.assertEqual(decoded(pane), "distrobox enter dev -- sh -c 'cd ~\nrc=0\nexit $rc'")
        pane = xssh.op_pane_command(Login("b", ["cd src", "exec zsh"]), "x")
        self.assertTrue(pane.endswith(" | base64 -d)\"; cd src; exec zsh'"), pane)
        # a login without commands, and localhost, end in a shell as the plain pane does
        pane = xssh.op_pane_command(Login("c"), "x")
        self.assertTrue(pane.endswith(" | base64 -d)\"; cd ~; exec $SHELL'"), pane)
        local = xssh.op_pane_command(Login("localhost"), "rc=0\nexit $rc")
        self.assertTrue(local.startswith('sh -c "$(echo '), local)
        self.assertTrue(local.endswith(' | base64 -d)"; cd ~; exec $SHELL'), local)
        self.assertEqual(decoded(local), "cd ~\nrc=0\nexit $rc")

    def test_the_decoded_pane_runs(self):
        pane = xssh.op_pane_command(Login("localhost"), "echo hi\nexit 3")
        inner = pane.split("; cd ~; exec $SHELL")[0]
        proc = subprocess.run(["sh", "-c", inner + "; echo rc=$?"], capture_output=True, text=True)
        self.assertEqual(proc.stdout, "hi\nrc=3\n")

    def test_logins_without_a_share_get_no_pane(self):
        entries = [Login("a", attributes={"mise": None}), Login("b"), Login("localhost")]
        kept, commands, skipped = xssh.op_panes(entries, self.OPS[:1], [])
        self.assertEqual(([e.login for e in kept], skipped), (["a"], ["b", "localhost"]))
        self.assertIn("== up", decoded(commands[0]))
        kept, commands, skipped = xssh.op_panes(entries, self.OPS, self.PATHS)
        self.assertEqual(([e.login for e in kept], skipped), (["a", "b", "localhost"], []))
        self.assertIn("\nrun p\n", decoded(commands[1]))
        self.assertNotIn("run q", decoded(commands[1]))
        self.assertNotIn("== up", decoded(commands[1]))


class Main(unittest.TestCase):
    def test_op_mode_execs_a_pane_per_taking_login(self):
        found = lists.Lists([Login("a", attributes={"mise": None}), Login("b")],
                            [PathInfo("p", attributes={"git": None})],
                            {op.name: op for op in OpPanes.OPS}, ["F"])
        with mock.patch.object(lists, "load_all", return_value=found) as load, \
                mock.patch.object(logins, "select", return_value=found.logins + [Login("localhost")]) as h, \
                mock.patch("shutil.which", return_value="/usr/bin/xpanes"), \
                mock.patch("os.execvp") as ex, \
                mock.patch.object(logins, "confirm_new_hosts") as confirm, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(xssh.main(["--stay", "-o", "up", "-f", "F"]), 0)
        load.assert_called_once_with(("F",))
        self.assertEqual(h.call_args.kwargs, {"found": found})
        confirm.assert_called_once_with(["a"], "xssh")
        argv = ex.call_args[0][1]
        self.assertEqual(argv[:4], ["xpanes", "--stay", "-e", argv[3]])
        self.assertEqual(len(argv), 4)
        self.assertIn("== up", decoded(argv[3]))
        self.assertEqual(err.getvalue(), "xssh: b: no operation applies, no pane\n"
                                         "xssh: localhost: no operation applies, no pane\n")
        with mock.patch.object(lists, "load_all", return_value=found), \
                mock.patch.object(logins, "select", return_value=[Login("b")]), \
                mock.patch("shutil.which", return_value="/usr/bin/xpanes"), \
                mock.patch("os.execvp") as ex, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(xssh.main(["-o", "up"]), 1)
        ex.assert_not_called()
        self.assertIn("xssh: no login takes part", err.getvalue())
        with mock.patch.object(lists, "load_all", return_value=found), \
                mock.patch("shutil.which", return_value="/usr/bin/xpanes"), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(xssh.main(["-o", "nope"]), 1)
        self.assertIn("unknown operation 'nope'", err.getvalue())

    def test_execs_xpanes(self):
        with mock.patch.object(logins, "select", return_value=[Login("a"), Login("localhost")]) as h, \
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
        with mock.patch.object(logins, "select", return_value=entries), \
                mock.patch("shutil.which", return_value="/usr/bin/xpanes"), \
                mock.patch("os.execvp") as ex, \
                mock.patch.object(xssh, "xpanes_expands_ampersand", return_value=True) as bash, \
                mock.patch.object(logins, "confirm_new_hosts"):
            xssh.main([])
        bash.assert_called_once()
        self.assertEqual(ex.call_args[0][1][-2:], ["ssh -t a 'cd x \\&\\& exec zsh'", "cd ~; exec $SHELL"])
        with mock.patch.object(logins, "select", return_value=[Login("a")]), \
                mock.patch("shutil.which", return_value="/usr/bin/xpanes"), \
                mock.patch("os.execvp"), \
                mock.patch.object(xssh, "xpanes_expands_ampersand") as bash, \
                mock.patch.object(logins, "confirm_new_hosts"):
            xssh.main([])
        bash.assert_not_called()

    def test_op_panes_escape_the_logins_commands_too(self):
        # The op pane ends in the login's commands, typed through the same
        # xpanes substitution as a plain pane's; it used to go unescaped.
        found = lists.Lists([Login("a", ["cd x && exec zsh"], attributes={"mise": None})],
                            [], {"up": OpPanes.OPS[0]}, ["F"])
        with mock.patch.object(lists, "load_all", return_value=found), \
                mock.patch.object(logins, "select", return_value=found.logins), \
                mock.patch("shutil.which", return_value="/usr/bin/xpanes"), \
                mock.patch("os.execvp") as ex, \
                mock.patch.object(xssh, "xpanes_expands_ampersand", return_value=True) as bash, \
                mock.patch.object(logins, "confirm_new_hosts"):
            self.assertEqual(xssh.main(["-o", "up"]), 0)
        bash.assert_called_once()
        pane = ex.call_args[0][1][-1]
        self.assertTrue(pane.endswith(" | base64 -d)\"; cd x \\&\\& exec zsh'"), pane)
        self.assertIn("== up", decoded(pane))

    def test_missing_xpanes_and_abort(self):
        with mock.patch("shutil.which", return_value=None), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(xssh.main([]), 1)
        self.assertIn("xpanes not installed", err.getvalue())
        with mock.patch("shutil.which", return_value="/usr/bin/xpanes"), \
                mock.patch.object(logins, "select", side_effect=pick.Abort), \
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
