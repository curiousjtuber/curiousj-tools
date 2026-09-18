import io
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from curiousj_tools import logins, pick, pssh
from curiousj_tools.lists import Login, PathInfo


def dry(argv, entries=("h1",), path_list=()):
    """pssh -n with the lists mocked: (exit status, stdout, stderr, the LoginOpts asked for)."""
    entries = [Login(e) if isinstance(e, str) else e for e in entries]
    with mock.patch.object(logins, "logins", return_value=entries) as h, \
            mock.patch.object(pssh, "paths_list", return_value=list(path_list)), \
            mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
            mock.patch("sys.stderr", new_callable=io.StringIO) as err:
        rc = pssh.main(argv)
    opts = h.call_args[0][0] if h.called else None
    return rc, out.getvalue(), err.getvalue(), opts


class Arguments(unittest.TestCase):
    def test_leading_flags_then_command(self):
        rc, out, _, opts = dry(["-N", "-f", "F", "-i", "-n", "uptime", "-p"])
        self.assertEqual(rc, 0)
        self.assertEqual(opts, logins.LoginOpts(no_local=True, file="F"))
        self.assertTrue(out.startswith("zsh -ic 'cd ~\nuptime -p'\n"), out)

    def test_twin_logins_run_once(self):
        # One login listed twice, for two sets of xssh commands, is one
        # machine to a batch run.
        _, out, _, _ = dry(["-n", "true"], entries=[Login("a"), "b", Login("a", ["exec zsh"])])
        self.assertEqual(out, "cd ~\ntrue\n-- on:\n   a\n   b\n")

    def test_via_adds_a_run_in_that_place(self):
        via = "distrobox enter dev --"
        entries = [Login("a"), Login("a", ["x"], via), "b", Login("b", via=via), Login("a", via=via)]
        _, out, _, _ = dry(["-n", "true"], entries=entries)
        self.assertEqual(out, "cd ~\ntrue\n-- on:\n   a\n   b\n"
                              "distrobox enter dev -- sh -c 'cd ~\ntrue'\n-- on:\n   a\n   b\n")
        _, out, _, _ = dry(["-ni", "true"], entries=[Login("a", via=via)])
        self.assertEqual(out, "distrobox enter dev -- zsh -ic 'cd ~\ntrue'\n-- on:\n   a\n")

    def test_short_flags_combine(self):
        rc, out, _, opts = dry(["-nNi", "uptime"])
        self.assertEqual((rc, opts), (0, logins.LoginOpts(no_local=True)))
        self.assertTrue(out.startswith("zsh -ic "), out)

    def test_path_flags_imply_paths(self):
        for flags in (["-P"], ["--paths"], ["-c"], ["-C"], ["--pick-paths"], ["-cC"]):
            rc, out, _, _ = dry(["-n", *flags, "x"], path_list=[PathInfo("a", "u")])
            self.assertEqual(rc, 0)
            self.assertIn("\nrun a" + (" u\n" if flags[0] in ("-c", "-cC") else "\n"), out, flags)
        _, out, _, _ = dry(["-n", "x"], path_list=[PathInfo("a")])
        self.assertNotIn("run a", out)

    def test_double_dash_starts_the_command(self):
        rc, out, _, opts = dry(["-n", "-p", "--", "-x", "y"])
        self.assertEqual((rc, opts.pick), (0, True))
        self.assertIn("\n-x y\n", out)

    def test_bad_flags_and_no_command(self):
        rc, _, err, opts = dry(["-x", "uptime"])
        self.assertEqual((rc, opts), (2, None))
        self.assertIn("No such option '-x'", err)
        rc, _, err, _ = dry(["-N"])
        self.assertEqual(rc, 2)
        self.assertIn("Missing argument", err)
        self.assertIn("pssh --help", err)


class CommandLine(unittest.TestCase):
    def test_single_word_is_a_shell_line(self):
        self.assertEqual(pssh.command_line(["cd ~/src && git pull"]), "cd ~/src && git pull")

    def test_several_words_are_quoted_argv(self):
        self.assertEqual(pssh.command_line(["ls", "my dir"]), "ls 'my dir'")

    def test_shell_command_starts_at_home_and_wraps_for_i(self):
        self.assertEqual(pssh.shell_command("uptime"), "cd ~\nuptime")
        cmd = pssh.shell_command("alias ec", interactive=True)
        self.assertTrue(cmd.startswith("zsh -ic '"))
        self.assertIn("alias ec", cmd)


def D(path, url=None, branch=None):
    return PathInfo(path, url, branch)


def sh(script, env=None):
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, env=env)


class DirsScript(unittest.TestCase):
    def test_quotes_each_call_and_embeds_the_command(self):
        script = pssh.paths_script([D("dotfiles"), D("my dir", "u r l"), D("it's", "u", "b")],
                                  "git status -s", clone=True)
        self.assertIn("\nrun dotfiles\n", script)
        self.assertIn("\nrun 'my dir' 'u r l'\n", script)
        self.assertIn("""\nrun 'it'"'"'s' u b\n""", script)
        self.assertIn("git status -s", script)
        self.assertTrue(script.endswith("exit $rc"))

    def test_without_clone_urls_are_left_out(self):
        script = pssh.paths_script([D("a", "url", "b")], "true")
        self.assertIn("\nrun a\n", script)
        self.assertNotIn("url", script.split("run() {")[1].split("\n}")[1])

    def test_runs_under_sh(self):
        """Run in an existing dir, skip a missing one, fail on a failing one."""
        with tempfile.TemporaryDirectory() as home:
            os.mkdir(os.path.join(home, "ok"))
            os.mkdir(os.path.join(home, "bad"))
            env = dict(os.environ, HOME=home)
            script = pssh.paths_script([D("ok"), D("missing"), D("bad")],
                                      '[ "$(basename "$PWD")" != bad ] && echo in $PWD')
            proc = sh(script, env)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), [
            f"== {home}/ok", f"in {home}/ok",
            f"== {home}/missing: missing, skipped",
            f"== {home}/bad", f"== {home}/bad: FAILED"])

    def test_absolute_dirs_stay_absolute(self):
        self.assertIn("== /opt/x: missing, skipped", sh(pssh.paths_script([D("/opt/x")], "true")).stdout)

    def test_clone_then_run(self):
        """A missing dir with a url is cloned (on the branch asked for) and the
        command runs in the fresh clone; a bad url fails that entry only."""
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home, GIT_CONFIG_GLOBAL="/dev/null")
            origin = os.path.join(home, "origin")
            subprocess.run(["git", "init", "-q", "-b", "main", origin], check=True, env=env)
            git = ["git", "-C", origin, "-c", "user.name=t", "-c", "user.email=t@t"]
            subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "init"], check=True, env=env)
            subprocess.run(git + ["branch", "-q", "feature"], check=True, env=env)
            entries = [D("clone", origin, "feature"), D("nourl"), D("bad", os.path.join(home, "nope"))]
            proc = sh(pssh.paths_script(entries, "git branch --show-current", clone=True), env)
            self.assertTrue(os.path.isdir(os.path.join(home, "clone", ".git")))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(lines[:3], [f"== {home}/clone: cloning {origin}", f"== {home}/clone", "feature"])
        self.assertIn(f"== {home}/nourl: missing, skipped", lines)
        self.assertIn(f"== {home}/bad: clone FAILED", lines)
        self.assertNotIn(f"== {home}/bad", lines)


TOML = '''# mine
logins = ["h1"]

[[paths]]
path = "a"
git_url = "git@example.com:me/a.git"
git_branch = "main"

[[paths]]
path = "b"
'''


class Paths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = os.path.join(self.tmp.name, "ssh-lists.toml")
        self.write(TOML)

    def write(self, text):
        with open(self.file, "w") as f:
            f.write(text)

    def test_reads_and_picks(self):
        self.assertEqual(pssh.paths_list(self.file, False),
                         [D("a", "git@example.com:me/a.git", "main"), D("b")])
        with mock.patch.object(pick, "pick", return_value=["b"]) as p:
            self.assertEqual(pssh.paths_list(self.file, True), [D("b")])
        p.assert_called_once_with(["a", "b"], "paths")

    def test_found_on_the_search_path(self):
        env = {"HOME": "/nonexistent", "SSH_LISTS_PATH": self.tmp.name}
        self.assertEqual(len(pssh.paths_list(None, False, env)), 2)

    def test_empty_and_missing(self):
        self.write("logins = ['h1']\n")
        with self.assertRaises(logins.ToolError) as cm:
            pssh.paths_list(self.file, False)
        self.assertIn("no paths in", str(cm.exception))
        with self.assertRaises(logins.ToolError):
            pssh.paths_list(None, False, env={"HOME": self.tmp.name})


class Wrap(unittest.TestCase):
    def test_plain_is_shell_command(self):
        self.assertEqual(pssh.wrap("uptime", None), pssh.shell_command("uptime"))
        self.assertEqual(pssh.wrap("uptime", None, True), pssh.shell_command("uptime", True))

    def test_via_gets_a_command_line(self):
        self.assertEqual(pssh.wrap("uptime", "distrobox enter dev --"),
                         "distrobox enter dev -- sh -c 'cd ~\nuptime'")
        self.assertEqual(pssh.wrap("uptime", "distrobox enter dev --", True),
                         "distrobox enter dev -- zsh -ic 'cd ~\nuptime'")

    def test_the_wrapped_script_runs(self):
        # `env HOME=...` standing in for a container with its own home: the
        # paths script survives the extra layer of quoting, `~` is that
        # home, and the exit status comes back through it.
        with tempfile.TemporaryDirectory() as home:
            os.mkdir(os.path.join(home, "ok"))
            line = pssh.wrap(pssh.paths_script([D("ok"), D("bad")], "echo in $PWD"), f"env HOME={home}")
            proc = sh(line)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.splitlines(),
                         [f"== {home}/ok", f"in {home}/ok", f"== {home}/bad: missing, skipped"])


class ParallelArgv(unittest.TestCase):
    def test_localhost_is_colon(self):
        argv = pssh.parallel_argv(["a", "b", "localhost"], "uptime")
        self.assertEqual(argv, ["parallel", "--nonall", "--tag", "--linebuffer",
                                "-S", "a,b,:", "uptime"])


class Main(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = os.path.join(self.tmp.name, "ssh-lists.toml")
        with open(self.file, "w") as f:
            f.write(TOML)

    def test_execs_parallel(self):
        with mock.patch.object(logins, "logins", return_value=[Login("a"), Login("localhost")]), \
                mock.patch("shutil.which", return_value="/usr/bin/parallel"), \
                mock.patch("os.execvp") as ex, \
                mock.patch.object(logins, "confirm_new_hosts"):
            pssh.main(["-N", "uptime"])
        ex.assert_called_once_with("parallel", ["parallel", "--nonall", "--tag", "--linebuffer",
                                                "-S", "a,:", "cd ~\nuptime"])

    def test_two_places_are_two_parallels(self):
        via = "distrobox enter dev --"
        entries = [Login("a"), Login("a", via=via), Login("localhost")]
        procs = [mock.Mock(wait=mock.Mock(return_value=1)), mock.Mock(wait=mock.Mock(return_value=2))]
        with mock.patch.object(logins, "logins", return_value=entries), \
                mock.patch("shutil.which", return_value="/usr/bin/parallel"), \
                mock.patch("os.execvp") as ex, \
                mock.patch.object(pssh.subprocess, "Popen", side_effect=procs) as popen, \
                mock.patch.object(logins, "confirm_new_hosts") as confirm:
            self.assertEqual(pssh.main(["uptime"]), 3)
        ex.assert_not_called()
        confirm.assert_called_once_with(["a", "localhost"], "pssh")
        self.assertEqual([c.args[0] for c in popen.call_args_list], [
            ["parallel", "--nonall", "--tag", "--linebuffer", "-S", "a,:", "cd ~\nuptime"],
            ["parallel", "--nonall", "--tag", "--linebuffer", "-S", "a",
             "distrobox enter dev -- sh -c 'cd ~\nuptime'"]])

    def test_dirs_mode_wraps_the_command(self):
        with mock.patch.object(logins, "logins", return_value=[Login("localhost")]), \
                mock.patch("shutil.which", return_value="/usr/bin/parallel"), \
                mock.patch("os.execvp") as ex, \
                mock.patch.object(logins, "confirm_new_hosts"):
            pssh.main(["-P", "-f", self.file, "git", "status", "-s"])
        cmd = ex.call_args[0][1][-1]
        self.assertTrue(cmd.startswith("cd ~\nrc=0\nrun() {"))
        self.assertIn("\nrun a\nrun b\nexit $rc", cmd)
        self.assertIn("git status -s", cmd)

    def test_clone_flag_reaches_the_script(self):
        with mock.patch.object(logins, "logins", return_value=[Login("localhost")]), \
                mock.patch("shutil.which", return_value="/usr/bin/parallel"), \
                mock.patch("os.execvp") as ex, \
                mock.patch.object(logins, "confirm_new_hosts"):
            pssh.main(["-c", "-f", self.file, "true"])
        self.assertIn("\nrun a git@example.com:me/a.git main\nrun b\n", ex.call_args[0][1][-1])

    def test_dry_run_prints_command_and_hosts_without_parallel(self):
        with mock.patch.object(logins, "logins", return_value=[Login("h1"), Login("localhost")]), \
                mock.patch("shutil.which", return_value=None), \
                mock.patch("os.execvp") as ex, \
                mock.patch.object(logins, "confirm_new_hosts"), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(pssh.main(["-n", "-P", "-f", self.file, "uptime"]), 0)
        ex.assert_not_called()
        text = out.getvalue()
        self.assertIn("\nrun a\nrun b\n", text)
        self.assertIn("-- on:\n   h1\n   localhost\n", text)

    def test_pick_dirs_before_hosts(self):
        with mock.patch.object(pick, "pick", side_effect=pick.Abort), \
                mock.patch.object(logins, "logins") as h, \
                mock.patch("shutil.which", return_value="/usr/bin/parallel"):
            self.assertEqual(pssh.main(["-C", "-f", self.file, "uptime"]), 130)
        h.assert_not_called()

    def test_missing_parallel_and_file(self):
        with mock.patch("shutil.which", return_value=None), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(pssh.main(["uptime"]), 1)
            with mock.patch("shutil.which", return_value="/usr/bin/parallel"):
                self.assertEqual(pssh.main(["-P", "-f", os.path.join(self.tmp.name, "none"), "x"]), 1)
        self.assertIn("parallel not installed", err.getvalue())
        self.assertIn("cannot read", err.getvalue())

    def test_unconfirmed_host_key_aborts_before_parallel(self):
        with mock.patch.object(logins, "logins", return_value=[Login("new"), Login("localhost")]), \
                mock.patch("shutil.which", return_value="/usr/bin/parallel"), \
                mock.patch.object(logins, "confirm_new_hosts",
                                  side_effect=logins.ToolError("new: host key not confirmed")), \
                mock.patch("os.execvp") as ex, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(pssh.main(["uptime"]), 1)
        ex.assert_not_called()
        self.assertIn("pssh: new: host key not confirmed", err.getvalue())

    def test_abort_propagates(self):
        with mock.patch("shutil.which", return_value="/usr/bin/parallel"), \
                mock.patch.object(logins, "logins", side_effect=pick.Abort):
            self.assertEqual(pssh.main(["-p", "uptime"]), 130)

    def test_help(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(pssh.main(["-h"]), 0)
        self.assertIn("pssh [-h]", out.getvalue())


if __name__ == "__main__":
    unittest.main()
