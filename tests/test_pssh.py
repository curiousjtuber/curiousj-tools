import io
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from curiousj_tools import attrs, lists, logins, pick, pssh
from curiousj_tools.lists import Lists, Login, Operation, PathInfo


def dry(argv, entries=("h1",), path_list=(), ops=()):
    """pssh -n with the lists mocked: (exit status, stdout, stderr, the LoginOpts asked for)."""
    entries = [Login(e) if isinstance(e, str) else e for e in entries]
    found = Lists(entries, list(path_list), {op.name: op for op in ops}, ["F"])
    with mock.patch.object(logins, "logins", return_value=entries) as h, \
            mock.patch.object(lists, "load_all", return_value=found), \
            mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
            mock.patch("sys.stderr", new_callable=io.StringIO) as err:
        rc = pssh.main(argv)
    opts = h.call_args[0][0] if h.called else None
    return rc, out.getvalue(), err.getvalue(), opts


def cond(text):
    return attrs.condition(text, "T")


class Arguments(unittest.TestCase):
    def test_leading_flags_then_command(self):
        rc, out, _, opts = dry(["-N", "-f", "F", "-i", "-n", "uptime", "-p"])
        self.assertEqual(rc, 0)
        self.assertEqual(opts, logins.LoginOpts(no_local=True, files=("F",)))
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
            with mock.patch.object(pick, "pick", return_value=["a"]):
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
        self.assertIn("Missing argument 'COMMAND...' (or -o NAME)", err)
        self.assertIn("pssh --help", err)

    def test_op_and_command_are_alternatives(self):
        rc, _, err, opts = dry(["-n", "-o", "x", "uptime"])
        self.assertEqual((rc, opts), (2, None))
        self.assertIn("COMMAND and -o NAME are alternatives", err)
        rc, _, err, _ = dry(["-n", "-P", "-o", "x"])
        self.assertEqual(rc, 2)
        self.assertIn("-P and -o", err)

    def test_path_attr_implies_paths_and_filters(self):
        paths = [PathInfo("a", attributes={"git": None}), PathInfo("b")]
        rc, out, _, _ = dry(["-n", "-A", "git", "x"], path_list=paths)
        self.assertEqual(rc, 0)
        self.assertIn("\nrun a\n", out)
        self.assertNotIn("run b", out)
        rc, _, err, _ = dry(["-n", "-A", "nope", "x"], path_list=paths)
        self.assertEqual(rc, 1)
        self.assertIn("no paths in F match -A nope", err)
        rc, _, err, _ = dry(["-n", "-A", "=", "x"], path_list=paths)
        self.assertEqual(rc, 2)
        self.assertIn("'-A' / '--path-attr'", err)


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

    def test_command_is_set_once_before_the_runs(self):
        script = pssh.paths_script([D("a"), D("b")], "echo it's")
        self.assertIn("\n}\ncmd='echo it'\"'\"'s'\nrun a\nrun b\nexit $rc", script)
        self.assertEqual(script.count("cmd="), 1)

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
        found = lists.load(self.file)
        self.assertEqual(pssh.paths_list(found), [D("a", "git@example.com:me/a.git", "main"), D("b")])
        with mock.patch.object(pick, "pick", return_value=["b"]) as p:
            self.assertEqual(pssh.paths_list(found, True), [D("b")])
        p.assert_called_once_with(["a", "b"], "paths")

    def test_terms_narrow_before_the_pick(self):
        found = lists.parse({"paths": [{"path": "a", "attributes": ["git"]}, "b"]}, "F")
        self.assertEqual(pssh.paths_list(found, terms=(attrs.Term("git"),)), [found.paths[0]])
        with mock.patch.object(pick, "pick", return_value=["b"]) as p:
            self.assertEqual(pssh.paths_list(found, True, (attrs.Term("git", negated=True),)),
                             [found.paths[1]])
        p.assert_called_once_with(["b"], "paths")

    def test_empty_is_an_error_naming_the_files(self):
        self.write("logins = ['h1']\n")
        with self.assertRaises(logins.ToolError) as cm:
            pssh.paths_list(lists.load(self.file))
        self.assertEqual(str(cm.exception), f"no paths in {self.file}")


GIT_PULL = Operation("git-pull", "git pull", paths=cond("git"), clone=True)
UP = Operation("up", "mise up", logins=cond("mise"))
ASK = Operation("ask", "sudo -v", logins=cond("cachyos"), serial=True)
SYS = Operation("sys", members=["up", "ask"])
ALL = Operation("all", members=["git-pull", "sys", "up"])
OPS = {op.name: op for op in (GIT_PULL, UP, ASK, SYS, ALL)}


class Expand(unittest.TestCase):
    def test_groups_open_depth_first_once_each(self):
        self.assertEqual([op.name for op in pssh.expand(["all"], OPS)], ["git-pull", "up", "ask"])
        self.assertEqual([op.name for op in pssh.expand(["up", "sys", "up"], OPS)], ["up", "ask"])
        self.assertEqual(pssh.expand(["ask"], OPS), [ASK])

    def test_unknown_operation_is_an_error(self):
        with self.assertRaises(logins.ToolError) as cm:
            pssh.expand(["nope"], OPS)
        self.assertEqual(str(cm.exception), "unknown operation 'nope'; known: all, ask, git-pull, sys, up")
        with self.assertRaises(logins.ToolError) as cm:
            pssh.expand(["x"], {})
        self.assertIn("known: none", str(cm.exception))

    def test_implicit_per_entry_operations_are_known(self):
        found = lists.merge([lists.parse({"logins": [{"login": "a", "operations": {"own": "ls"}}]})])
        self.assertEqual(pssh.expand(["own"], found.operations), [Operation("own")])


class LoginScript(unittest.TestCase):
    PATHS = [PathInfo("p", "u", attributes={"git": None}), PathInfo("q", "v"),
             PathInfo("r", operations={"git-pull": "git pull --ff-only"})]

    def lines(self, script):
        return script.split("\n}\n", 1)[1].splitlines()

    def test_per_login_op_runs_from_home_under_a_header(self):
        script = pssh.login_script(Login("a", attributes={"mise": None}), [UP], [])
        self.assertTrue(script.startswith("rc=0\nrun() {"))
        self.assertEqual(self.lines(script), [
            "echo '== up'", "cmd='mise up'", '( eval "$cmd" ) || { echo \'== up: FAILED\'; rc=1; }',
            "exit $rc"])
        self.assertIsNone(pssh.login_script(Login("a"), [UP], []))

    def test_per_path_op_uses_overrides_and_conditions(self):
        script = pssh.login_script(Login("a"), [GIT_PULL], self.PATHS)
        self.assertEqual(self.lines(script), [
            "echo '== git-pull'", "cmd='git pull'", "run p u",
            "cmd='git pull --ff-only'", "run r", "exit $rc"])
        script = pssh.login_script(Login("a"), [Operation("git-pull", "git pull", paths=cond("git"))],
                                   self.PATHS[:2])
        self.assertEqual(self.lines(script), ["echo '== git-pull'", "cmd='git pull'", "run p", "exit $rc"])
        self.assertIsNone(pssh.login_script(Login("a"), [GIT_PULL], self.PATHS[1:2]))

    def test_own_command_wins_and_needs_no_condition(self):
        entry = Login("a", attributes={"mise": None}, operations={"up": "brew up", "own": "ls"})
        script = pssh.login_script(entry, [UP, Operation("own")], [])
        self.assertEqual(self.lines(script)[:2], ["echo '== up'", "cmd='brew up'"])
        self.assertIn("cmd=ls", script)
        self.assertIsNone(pssh.login_script(Login("b"), [Operation("own")], []))

    def test_login_condition_gates_a_per_path_op(self):
        gated = Operation("git-pull", "git pull", logins=cond("dev"), paths=True and attrs.EVERYTHING)
        self.assertIsNone(pssh.login_script(Login("a"), [gated], self.PATHS))
        script = pssh.login_script(Login("a", attributes={"dev": None}), [gated], self.PATHS)
        self.assertEqual(self.lines(script).count("run p"), 1)

    def test_clone_from_the_op_or_the_flag(self):
        plain = Operation("gp", "git pull", paths=attrs.EVERYTHING)
        self.assertIn("\nrun p\nrun q\n", pssh.login_script(Login("a"), [plain], self.PATHS[:2]))
        self.assertIn("\nrun p u\nrun q v\n", pssh.login_script(Login("a"), [plain], self.PATHS[:2], clone=True))
        self.assertIn("\nrun p u\n", pssh.login_script(Login("a"), [GIT_PULL], self.PATHS[:1]))

    def test_runs_under_sh(self):
        """Headers, a per-path failure and a per-login failure, all reported,
        the exit status telling something failed."""
        ops = [Operation("gp", '[ "$(basename "$PWD")" != bad ] && echo in $PWD', paths=attrs.EVERYTHING),
               Operation("home", "echo at $PWD && false"), Operation("fine", "echo ok")]
        with tempfile.TemporaryDirectory() as home:
            os.mkdir(os.path.join(home, "ok"))
            os.mkdir(os.path.join(home, "bad"))
            env = dict(os.environ, HOME=home)
            script = pssh.login_script(Login("a"), ops, [D("ok"), D("bad")])
            proc = sh(pssh.wrap(script, None), env)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), [
            "== gp", f"== {home}/ok", f"in {home}/ok", f"== {home}/bad", f"== {home}/bad: FAILED",
            "== home", f"at {home}", "== home: FAILED", "== fine", "ok"])


class ListOperations(unittest.TestCase):
    def test_columns_places_flags_and_own_commands(self):
        found = Lists([Login("a", operations={"up": "brew up"}), Login("b")],
                      [PathInfo("p", operations={"git-pull": "git pull --ff-only"})],
                      {"git-pull": GIT_PULL, "up": UP, "ask": ASK, "sys": SYS,
                       "gated": Operation("gated", "ls", logins=cond("dev"), paths=attrs.EVERYTHING),
                       "own": Operation("own")}, ["F"])
        self.assertEqual(pssh.list_operations(found).splitlines(), [
            "git-pull  per path git, clone        git pull",
            "    p: git pull --ff-only",
            "up        per login mise             mise up",
            "    a: brew up",
            "ask       per login cachyos, serial  sudo -v",
            "sys       group                      up, ask",
            "gated     per path on logins dev     ls",
            "own       per login                  (the entries' own commands)",
        ])
        self.assertEqual(pssh.list_operations(Lists(files=["F", "G"])), "no operations in F, G")

    def test_flag_prints_and_exits(self):
        rc, out, err, opts = dry(["-L", "-f", "F"], ops=[UP])
        self.assertEqual((rc, err, opts), (0, "", None))
        self.assertEqual(out, "up  per login mise  mise up\n")
        rc, out, err, _ = dry(["-L"])
        self.assertEqual((rc, out, err), (0, "no operations in F\n", ""))


class Serial(unittest.TestCase):
    def test_localhost_is_a_local_shell(self):
        self.assertEqual(pssh.serial_argv("a", "cd ~\nuptime"), ["ssh", "-t", "a", "cd ~\nuptime"])
        self.assertEqual(pssh.serial_argv("localhost", "x"), ["sh", "-c", "x"])

    def test_each_login_in_turn_counting_failures(self):
        groups = {"one": ["a", "localhost"], "two": ["b"]}
        results = [mock.Mock(returncode=0), mock.Mock(returncode=1), mock.Mock(returncode=2)]
        with mock.patch.object(pssh.subprocess, "run", side_effect=results) as run, \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(pssh.serial_run(groups), 2)
        self.assertEqual([c.args[0] for c in run.call_args_list],
                         [["ssh", "-t", "a", "one"], ["sh", "-c", "one"], ["ssh", "-t", "b", "two"]])
        self.assertEqual(out.getvalue(), "== a\n== localhost\n== b\n")
        with mock.patch.object(pssh.subprocess, "run", side_effect=KeyboardInterrupt), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(pssh.serial_run(groups), 130)


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

    def test_ops_group_identical_scripts_and_report_the_skipped(self):
        entries = [Login("a", attributes={"mise": None}), Login("b", attributes={"mise": None}),
                   Login("c"), Login("localhost")]
        rc, out, err, opts = dry(["-n", "-o", "up"], entries=entries, ops=OPS.values())
        self.assertEqual((rc, err), (0, ""))
        self.assertTrue(out.startswith("cd ~\nrc=0\nrun() {"), out)
        self.assertIn("\necho '== up'\ncmd='mise up'\n", out)
        self.assertEqual(out.count("== up'"), 1)
        self.assertIn("\n-- on:\n   a\n   b\n-- not contacted (no operation applies):\n   c\n   localhost\n", out)
        # a per-path op reads the paths; a run with none leaves them alone
        rc, _, err, _ = dry(["-n", "-o", "git-pull"], entries=entries, ops=OPS.values())
        self.assertEqual(rc, 1)
        self.assertIn("no paths in F", err)
        rc, out, _, _ = dry(["-n", "-o", "git-pull", "-A", "git"], entries=entries[2:],
                            path_list=[PathInfo("p", "u", attributes={"git": None}), PathInfo("q")],
                            ops=OPS.values())
        self.assertEqual(rc, 0)
        self.assertIn("\necho '== git-pull'\ncmd='git pull'\nrun p u\nexit $rc\n-- on:\n   c\n   localhost\n", out)

    def test_a_serial_op_makes_the_run_serial(self):
        entries = [Login("a", attributes={"cachyos": None}), Login("b", attributes={"mise": None})]
        rc, out, err, _ = dry(["-n", "-o", "sys"], entries=entries, ops=OPS.values())
        self.assertEqual(rc, 0)
        self.assertEqual(err, "pssh: ask asks questions: one login at a time\n")
        self.assertIn("\n-- one at a time on:\n   a\n", out)
        self.assertIn("\n-- one at a time on:\n   b\n", out)
        rc, out, err, _ = dry(["-n", "-s", "up"], entries=entries)
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("\n-- one at a time on:\n   a\n   b\n", out)

    def test_serial_runs_in_the_foreground_without_parallel(self):
        entries = [Login("a"), Login("localhost")]
        found = Lists(entries, [], {"ask": ASK}, ["F"])
        with mock.patch.object(logins, "logins", return_value=[Login("a", attributes={"cachyos": None}),
                                                               Login("localhost")]), \
                mock.patch.object(lists, "load_all", return_value=found), \
                mock.patch("shutil.which", return_value=None), \
                mock.patch("os.execvp") as ex, \
                mock.patch.object(pssh.subprocess, "run", return_value=mock.Mock(returncode=1)) as run, \
                mock.patch.object(logins, "confirm_new_hosts") as confirm, \
                mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(pssh.main(["-o", "ask"]), 1)
        ex.assert_not_called()
        confirm.assert_called_once_with(["a"], "pssh")
        self.assertEqual(run.call_args.args[0][:3], ["ssh", "-t", "a"])
        self.assertIn("pssh: localhost: no operation applies, skipped", err.getvalue())
        with mock.patch.object(logins, "logins", return_value=[Login("a")]), \
                mock.patch("shutil.which", return_value=None), \
                mock.patch.object(pssh.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, \
                mock.patch.object(logins, "confirm_new_hosts"), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(pssh.main(["-s", "uptime"]), 0)
        self.assertEqual(run.call_args.args[0], ["ssh", "-t", "a", "cd ~\nuptime"])

    def test_localhost_runs_the_self_entrys_operations(self):
        with open(self.file, "w") as f:
            f.write('logins = [{ login = "alice@my-mac.local", attributes = ["mise"], '
                    'operations = { up = "brew up" } }, "b"]\n'
                    '[operations.up]\ncommand = "mise up"\nlogins = "mise"\n')
        with mock.patch.object(logins, "self_names", return_value={"my-mac", "localhost"}), \
                mock.patch("getpass.getuser", return_value="alice"), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(pssh.main(["-n", "-o", "up", "-f", self.file]), 0)
        self.assertEqual(err.getvalue(), "")
        self.assertIn("cmd='brew up'\n", out.getvalue())
        self.assertIn("\n-- on:\n   localhost\n-- not contacted (no operation applies):\n   b\n", out.getvalue())

    def test_op_mode_reads_the_files_once(self):
        found = Lists([Login("a")], [], {"up": UP}, ["F"])
        with mock.patch.object(lists, "load_all", return_value=found) as load, \
                mock.patch.object(logins, "self_names", return_value=set()), \
                mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(pssh.main(["-n", "-o", "up", "-f", "F", "-f", "G"]), 0)
        load.assert_called_once_with(("F", "G"))

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
