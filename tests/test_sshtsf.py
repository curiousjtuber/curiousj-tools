"""sshtsf: config round trip, alias resolution, dry-run command lines.

Nothing here touches ssh, tmux or Emacs: connect() is exercised only with
--dry-run, and the Emacs socket probe is patched where a forward is wanted.
"""

from __future__ import annotations

import contextlib
import io
import os
import shlex
import tempfile
import unittest
from unittest import mock

from curiousj_tools import sshtsf

SAMPLE = {
    "default_host": "devbox",
    "last": {"host": "devbox", "session": "web"},
    "hosts": {
        "devbox": {
            "alias": "c",
            "ecf": False,
            "sessions": {
                "web": {"alias": "devweb", "folder": "src/webapp",
                        "command": "make dev"},
                "api": {"folder": "src/api", "ecf": True},
                "shell": {},
            },
        },
        "build": {
            "target": "build.internal",
            "ecf_port": 41234,
            "sessions": {},
        },
    },
}


REMOTE_PATH_LINE = ('PATH="/opt/homebrew/bin:/usr/local/bin:'
                    '/home/linuxbrew/.linuxbrew/bin:$HOME/.local/bin:$PATH"; ')


def no_tmux(folder: str = "", env: tuple[tuple[str, str], ...] = ()) -> str:
    """The remote script's opening: the PATH, then what happens without tmux."""
    return (REMOTE_PATH_LINE + 'command -v tmux >/dev/null || { '
            'echo "sshtsf: no tmux on the remote; a plain shell instead" >&2; '
            + ("cd %s 2>/dev/null; " % folder if folder else "")
            + "".join("export %s=%s; " % pair for pair in env)
            + 'exec "${SHELL:-sh}" -l; }; ')


def run_capture(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = sshtsf.main(argv)
        except SystemExit as exc:
            # sys.exit("message") prints the message on the way out; collect
            # it as stderr, the way the shell would see it.
            if isinstance(exc.code, int) or exc.code is None:
                rc = exc.code or 0
            else:
                rc = 1
                err.write(str(exc.code) + "\n")
    return rc, out.getvalue(), err.getvalue()


class ConfigDirMixin:
    """A throwaway config dir, patched into the module-level constants."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg_dir = os.path.join(self.tmp.name, "sshtsf")
        cfg_path = os.path.join(cfg_dir, "config.toml")
        for name, value in (("CONFIG_DIR", cfg_dir), ("CONFIG_PATH", cfg_path)):
            patcher = mock.patch.object(sshtsf, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cfg_path = cfg_path


class TestHelp(unittest.TestCase):
    def test_help_exits_zero_and_uses_generic_names(self):
        rc, out, _ = run_capture(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("devbox", out)
        self.assertIn("sshtsf remote devbox", out)
        self.assertIn("forwarding the local Emacs socket", out)

    def test_contradictory_flags_are_rejected(self):
        rc, _, err = run_capture(["-e", "-E", "devbox"])
        self.assertEqual(rc, 2)
        self.assertIn("contradictory", err)


class TestConfigRoundTrip(ConfigDirMixin, unittest.TestCase):
    def test_missing_config_loads_empty(self):
        self.assertEqual(sshtsf.load_config(), {"hosts": {}})

    def test_save_then_load_preserves_fields(self):
        sshtsf.save_config(SAMPLE)
        self.assertTrue(os.path.exists(self.cfg_path))
        loaded = sshtsf.load_config()
        self.assertEqual(loaded["default_host"], "devbox")
        self.assertEqual(loaded["last"], {"host": "devbox", "session": "web"})
        web = loaded["hosts"]["devbox"]["sessions"]["web"]
        self.assertEqual(web, {"alias": "devweb", "folder": "src/webapp",
                               "command": "make dev"})
        # A false boolean survives a rewrite: it is what opts a session out of
        # a host default, so it is keyed on presence rather than truth.
        self.assertIs(loaded["hosts"]["devbox"]["ecf"], False)
        self.assertIs(loaded["hosts"]["devbox"]["sessions"]["api"]["ecf"], True)
        self.assertEqual(loaded["hosts"]["build"]["target"], "build.internal")
        self.assertEqual(loaded["hosts"]["build"]["ecf_port"], 41234)
        # Round-tripping the loaded config is a fixed point.
        self.assertEqual(sshtsf.dump_config(loaded), sshtsf.dump_config(SAMPLE))

    def test_dump_quotes_awkward_keys_and_values(self):
        cfg = {"hosts": {"my host": {"sessions": {
            "s": {"command": 'say "hi"\tnow', "folder": "a b"}}}}}
        text = sshtsf.dump_config(cfg)
        self.assertIn('[hosts."my host"]', text)
        self.assertIn('[hosts."my host".sessions.s]', text)
        self.assertIn('command = "say \\"hi\\"\\tnow"', text)
        sshtsf.save_config(cfg)
        self.assertEqual(sshtsf.load_config()["hosts"]["my host"]["sessions"]["s"],
                         cfg["hosts"]["my host"]["sessions"]["s"])

    def test_invalid_toml_is_reported(self):
        os.makedirs(sshtsf.CONFIG_DIR)
        with open(self.cfg_path, "w") as fh:
            fh.write("hosts = [\n")
        with self.assertRaises(SystemExit) as ctx:
            sshtsf.load_config()
        self.assertIn("not valid TOML", str(ctx.exception))

    def test_config_path_subcommand(self):
        rc, out, _ = run_capture(["config-path"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), self.cfg_path)


def scripted(answers: dict[str, str]):
    """An `ask' that answers by prompt prefix, and fails on a prompt it lacks."""
    def fake_ask(prompt, default=""):
        for prefix, reply in answers.items():
            if prompt.strip().startswith(prefix):
                return reply or default
        raise AssertionError("unexpected prompt: %r" % prompt)
    return fake_ask


class TestHostCandidates(unittest.TestCase):
    def test_fresh_first_then_registered_with_their_names(self):
        cfg = {"hosts": {
            "devbox": {"alias": "c"},
            "build": {"target": "build.internal"},
            "mac": {"target": "me@mac.local"},
            "mac-root": {"target": "root@mac.local"},
        }}
        known = ["build.internal", "c", "devbox", "mac.local", "other"]
        with mock.patch.object(sshtsf, "ssh_known_hosts", return_value=known):
            got = sshtsf.ssh_host_candidates(cfg)
        self.assertEqual(got, [
            ("other", ""),
            ("build.internal", "registered as build"),
            ("c", "registered as devbox"),
            ("devbox", "registered as devbox"),
            ("mac.local", "registered as mac, mac-root"),
        ])


class TestAddHost(ConfigDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        sshtsf.save_config(SAMPLE)

    def test_picker_offers_registered_machines_last_and_maps_them_back(self):
        cfg = sshtsf.load_config()
        cfg["hosts"]["devbox"]["target"] = "me@devbox.local"
        sshtsf.save_config(cfg)
        answers = {"name for this [user@]host": "devbox-root", "ssh destination": "",
                   "user on it": "root", "alias": "",
                   "forward the local Emacs socket": "", "forward Wayland": ""}
        with mock.patch.object(sshtsf, "ssh_known_hosts",
                               return_value=["devbox.local", "other"]), \
             mock.patch.object(sshtsf, "pick",
                               return_value="devbox.local  (registered as devbox)") as pick, \
             mock.patch.object(sshtsf, "ask", side_effect=scripted(answers)), \
             mock.patch.object(sshtsf, "route_add_session", return_value=0):
            rc = sshtsf.route_add_host(sshtsf.load_config())
        self.assertEqual(rc, 0)
        self.assertEqual(pick.call_args.args[0],
                         ["other", "devbox.local  (registered as devbox)"])
        self.assertEqual(sshtsf.load_config()["hosts"]["devbox-root"],
                         {"target": "root@devbox.local"})

    def test_unknown_host_hint_lists_only_fresh_machines(self):
        with mock.patch.object(sshtsf, "ssh_known_hosts", return_value=["devbox", "other"]):
            rc, _, err = run_capture(["nowhere", "--dry-run"])
        self.assertEqual(rc, 1)
        self.assertIn("in ~/.ssh/known_hosts:\n          other\n", err)

    def add_host(self, answers, typed=""):
        with mock.patch.object(sshtsf, "ask", side_effect=scripted(answers)), \
             mock.patch.object(sshtsf, "ssh_host_candidates", return_value=[]), \
             mock.patch.object(sshtsf, "route_add_session", return_value=0) as nxt:
            rc = sshtsf.route_add_host(sshtsf.load_config(), typed)
        self.assertEqual(rc, 0)
        nxt.assert_called_once()
        return sshtsf.load_config()["hosts"][answers["name for this [user@]host"]]

    def test_destination_defaults_to_the_machine_typed_not_the_name(self):
        # `sshtsf add mac.local`, then shortened to "mac" at the name prompt:
        # a blank destination still dials mac.local.
        hcfg = self.add_host({"name for this [user@]host": "mac", "ssh destination": "",
                              "user on it": "", "alias": "",
                              "forward the local Emacs socket": "",
                              "forward Wayland": ""}, typed="mac.local")
        self.assertEqual(hcfg, {"target": "mac.local"})

    def test_asks_the_host_defaults_before_the_first_session(self):
        hcfg = self.add_host({"name for this [user@]host": "mac", "ssh destination": "me@mac.local",
                              "alias": "m", "forward the local Emacs socket": "y",
                              "forward Wayland": ""})
        self.assertEqual(hcfg, {"target": "me@mac.local", "alias": "m", "ecf": True})

    def test_a_no_leaves_the_fields_out(self):
        hcfg = self.add_host({"name for this [user@]host": "mac", "ssh destination": "",
                              "user on it": "", "alias": "",
                              "forward the local Emacs socket": "n",
                              "forward Wayland": "no"})
        self.assertEqual(hcfg, {})

    def test_user_is_asked_only_when_the_destination_names_none(self):
        # "me@mac.local" above went straight to the alias: no user prompt.
        hcfg = self.add_host({"name for this [user@]host": "mac", "ssh destination": "mac.local",
                              "user on it": "me", "alias": "",
                              "forward the local Emacs socket": "",
                              "forward Wayland": ""})
        self.assertEqual(hcfg, {"target": "me@mac.local"})

    def test_blank_user_leaves_the_destination_alone(self):
        hcfg = self.add_host({"name for this [user@]host": "mac", "ssh destination": "mac.local",
                              "user on it": "", "alias": "",
                              "forward the local Emacs socket": "",
                              "forward Wayland": ""})
        self.assertEqual(hcfg, {"target": "mac.local"})

    def test_session_on_such_a_host_is_not_asked_again(self):
        cfg = sshtsf.load_config()
        cfg["hosts"]["mac"] = {"ecf": True, "waypipe": True, "sessions": {}}
        sshtsf.save_config(cfg)
        # No "forward ..." entries: the scripted ask fails if either is asked.
        answers = {"session name": "s", "command to run": "", "alias for": ""}
        with mock.patch.object(sshtsf, "ask", side_effect=scripted(answers)), \
             mock.patch.object(sshtsf, "prompt_folder", return_value=""), \
             mock.patch.object(sshtsf, "connect", return_value=0):
            rc = sshtsf.route_add_session(cfg, "mac")
        self.assertEqual(rc, 0)
        self.assertEqual(sshtsf.load_config()["hosts"]["mac"]["sessions"]["s"],
                         {"alias": "s"})


class TestPromptFolder(unittest.TestCase):
    def prompt(self, typed, current="", dirs=("src/a", "src/b"), picked="src/b"):
        with mock.patch.object(sshtsf, "ask", side_effect=scripted({"folder": typed})) as ask, \
             mock.patch.object(sshtsf, "remote_dirs", return_value=list(dirs)) as ls, \
             mock.patch.object(sshtsf, "pick", return_value=picked) as pick:
            got = sshtsf.prompt_folder("devbox", current, label="web")
        return got, ask.call_args, ls.called, pick

    def test_blank_is_none(self):
        got, call, listed, _ = self.prompt("")
        self.assertEqual(got, "")
        self.assertFalse(listed)
        self.assertIn("(blank for none, ? to browse)", call.args[0])

    def test_typed_path_is_taken_as_is(self):
        got, _, listed, _ = self.prompt("src/webapp/")
        self.assertEqual((got, listed), ("src/webapp", False))

    def test_question_mark_browses(self):
        got, _, listed, pick = self.prompt("?")
        self.assertEqual((got, listed), ("src/b", True))
        self.assertEqual(pick.call_args.kwargs["query"], "")

    def test_updating_blank_keeps_and_dash_clears(self):
        got, call, _, _ = self.prompt("", current="src/old")
        self.assertEqual(got, "src/old")
        self.assertEqual(call.args[1], "src/old")
        self.assertIn("(? to browse, - for none)", call.args[0])
        got, _, _, _ = self.prompt("-", current="src/old")
        self.assertEqual(got, "")

    def test_updating_browse_starts_from_the_current(self):
        got, _, _, pick = self.prompt("?", current="src/old")
        self.assertEqual(got, "src/b")
        self.assertEqual(pick.call_args.kwargs["query"], "src/old")

    def test_aborted_browse_is_none(self):
        got, _, _, _ = self.prompt("?", picked=None)
        self.assertIsNone(got)


class TestEdit(ConfigDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        sshtsf.save_config(SAMPLE)

    def test_visual_wins_and_is_split_like_a_shell(self):
        with mock.patch.dict(os.environ, {"VISUAL": "emacsclient -t", "EDITOR": "nano"}):
            rc, out, _ = run_capture(["edit", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "emacsclient -t " + self.cfg_path)

    def test_editor_then_vi(self):
        with mock.patch.dict(os.environ, {"VISUAL": "", "EDITOR": "nano"}):
            _, out, _ = run_capture(["edit", "--dry-run"])
        self.assertEqual(out.strip(), "nano " + self.cfg_path)
        with mock.patch.dict(os.environ, {"VISUAL": "", "EDITOR": ""}):
            _, out, _ = run_capture(["edit", "--dry-run"])
        self.assertEqual(out.strip(), "vi " + self.cfg_path)

    def test_missing_config_is_created_first(self):
        os.remove(self.cfg_path)
        with mock.patch.dict(os.environ, {"VISUAL": "ed"}):
            rc, _, _ = run_capture(["edit", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(self.cfg_path))
        self.assertEqual(sshtsf.load_config(), {"hosts": {}})

    def test_runs_editor_and_reports_a_slip(self):
        def scribble(argv, **kw):
            with open(argv[-1], "w") as fh:
                fh.write("[hosts.devbox\n")
            return mock.Mock(returncode=0)

        with mock.patch.dict(os.environ, {"VISUAL": "ed"}), \
             mock.patch.object(sshtsf.subprocess, "run", side_effect=scribble) as run:
            rc, _, err = run_capture(["edit"])
        self.assertEqual(rc, 1)
        self.assertEqual(run.call_args.args[0], ["ed", self.cfg_path])
        self.assertIn("not valid TOML", err)

    def test_editor_failure_is_passed_on(self):
        with mock.patch.dict(os.environ, {"VISUAL": "ed"}), \
             mock.patch.object(sshtsf.subprocess, "run",
                               return_value=mock.Mock(returncode=3)):
            rc, _, err = run_capture(["edit"])
        self.assertEqual(rc, 3)
        self.assertIn("ed exited 3", err)

    def test_works_on_a_config_that_will_not_parse(self):
        with open(self.cfg_path, "w") as fh:
            fh.write("[hosts.devbox\n")
        # Every other verb stops at the parse error; edit is the way past it.
        rc, _, err = run_capture(["list"])
        self.assertEqual(rc, 1)
        self.assertIn("not valid TOML", err)

        def repair(argv, **kw):
            sshtsf.save_config(SAMPLE)
            return mock.Mock(returncode=0)

        with mock.patch.dict(os.environ, {"VISUAL": "ed"}), \
             mock.patch.object(sshtsf.subprocess, "run", side_effect=repair):
            rc, _, err = run_capture(["edit"])
        self.assertEqual(rc, 0)
        # Told why the editor opened, before it did.
        self.assertIn("not valid TOML", err)
        self.assertEqual(run_capture(["list"])[0], 0)


class TestResolution(unittest.TestCase):
    def test_host_by_key_and_alias(self):
        self.assertEqual(sshtsf.resolve_host(SAMPLE, "devbox"), "devbox")
        self.assertEqual(sshtsf.resolve_host(SAMPLE, "c"), "devbox")
        self.assertIsNone(sshtsf.resolve_host(SAMPLE, "nope"))

    def test_session_by_name_and_alias(self):
        self.assertEqual(sshtsf.resolve_session(SAMPLE, "devbox", "web"), "web")
        self.assertEqual(sshtsf.resolve_session(SAMPLE, "devbox", "devweb"), "web")
        self.assertIsNone(sshtsf.resolve_session(SAMPLE, "devbox", "zzz"))
        self.assertIsNone(sshtsf.resolve_session(SAMPLE, "build", "web"))

    def test_pair_alias(self):
        self.assertEqual(sshtsf.resolve_pair_alias(SAMPLE, "devweb"), ("devbox", "web"))
        self.assertIsNone(sshtsf.resolve_pair_alias(SAMPLE, "web"))

    def test_ssh_target_defaults_to_host_key(self):
        self.assertEqual(sshtsf.ssh_target(SAMPLE, "devbox"), "devbox")
        self.assertEqual(sshtsf.ssh_target(SAMPLE, "build"), "build.internal")

    def test_resolve_flag_order_is_override_session_host(self):
        cfg = {"hosts": {"h": {"ecf": True, "sessions": {"s": {"ecf": False}, "t": {}}}}}
        self.assertFalse(sshtsf.resolve_flag(cfg, "h", "s", "ecf", None))
        self.assertTrue(sshtsf.resolve_flag(cfg, "h", "t", "ecf", None))
        self.assertTrue(sshtsf.resolve_flag(cfg, "h", "s", "ecf", True))
        self.assertFalse(sshtsf.resolve_flag(cfg, "h", "t", "ecf", False))

    def test_describe_host_marks(self):
        self.assertEqual(sshtsf.describe_host(SAMPLE, "devbox"),
                         "devbox  (alias c, default)")
        self.assertEqual(sshtsf.describe_host(SAMPLE, "build"),
                         "build  (-> build.internal)")


class TestKnownHosts(unittest.TestCase):
    def test_parsing_skips_hashed_markers_and_patterns(self):
        with tempfile.NamedTemporaryFile("w", suffix="known_hosts", delete=False) as fh:
            fh.write("\n".join([
                "# comment",
                "devbox,10.0.0.5 ssh-ed25519 AAAA",
                "devbox ssh-rsa BBBB",
                "[build.internal]:2222 ssh-ed25519 CCCC",
                "|1|hash|hash ssh-ed25519 DDDD",
                "@cert-authority *.example.com ssh-rsa EEEE",
                "@revoked gone ssh-rsa FFFF",
                "*.wild ssh-rsa GGGG",
                "!negated ssh-rsa HHHH",
                "",
            ]))
            path = fh.name
        self.addCleanup(os.unlink, path)
        self.assertEqual(sshtsf.ssh_known_hosts(path),
                         ["10.0.0.5", "build.internal", "devbox"])

    def test_missing_file_is_empty(self):
        self.assertEqual(sshtsf.ssh_known_hosts("/nonexistent/known_hosts"), [])


class TestDryRun(ConfigDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        sshtsf.save_config(SAMPLE)

    def test_host_and_session(self):
        rc, out, _ = run_capture(["devbox", "web", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            out.strip(),
            "ssh -t devbox sh -c '" + no_tmux("src/webapp")
            + "exec tmux -u new-session -A -c src/webapp -s web make dev'")

    def test_pair_alias_and_host_alias(self):
        rc, out, _ = run_capture(["devweb", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("-s web make dev", out)
        rc, out, _ = run_capture(["c", "shell", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "ssh -t devbox sh -c '" + no_tmux()
                         + "exec tmux -u new-session -A -s shell'")

    def test_dry_run_does_not_touch_last_used(self):
        run_capture(["devbox", "shell", "--dry-run"])
        self.assertEqual(sshtsf.load_config()["last"],
                         {"host": "devbox", "session": "web"})

    def test_ecf_forward_from_session_config(self):
        with mock.patch.object(sshtsf, "ecf_local_socket",
                               return_value="/run/user/1000/emacs/server"):
            rc, out, _ = run_capture(["devbox", "api", "--dry-run"])
        self.assertEqual(rc, 0)
        lines = out.strip().splitlines()
        self.assertEqual(lines[0], "ssh devbox rm -f /tmp/emacs-remote-socket")
        self.assertEqual(
            lines[1],
            "ssh -o ExitOnForwardFailure=yes "
            "-R /tmp/emacs-remote-socket:/run/user/1000/emacs/server "
            "-t devbox sh -c '"
            + no_tmux("src/api", (("EMACS_REMOTE_TARGET", "devbox"),))
            + "tmux has-session -t =api 2>/dev/null "
            "|| tmux -u new-session -d -c src/api -s api || exit 1; "
            "tmux set-environment -t =api EMACS_REMOTE_TARGET devbox; "
            "exec tmux -u attach-session -t =api'")

    def test_ecf_tells_session_the_target_as_dialed(self):
        # The remote's emacs-remote builds its TRAMP prefix from this, so it
        # has to be the name the local Emacs can reach: the configured ssh
        # destination, user@ and all, not the host key.
        cfg = sshtsf.load_config()
        cfg["hosts"]["devbox"]["target"] = "me@devbox.local"
        sshtsf.save_config(cfg)
        with mock.patch.object(sshtsf, "ecf_local_socket", return_value="/x/server"):
            rc, out, _ = run_capture(["devbox", "api", "--dry-run"])
        self.assertEqual(rc, 0)
        cmd = out.strip().splitlines()[1]
        self.assertIn("-t me@devbox.local sh -c", cmd)
        self.assertIn("EMACS_REMOTE_TARGET me@devbox.local;", cmd)

    def test_without_ecf_the_session_is_not_told_a_target(self):
        rc, out, _ = run_capture(["devbox", "shell", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertNotIn("EMACS_REMOTE_TARGET", out)
        # Nothing to tell the session, so no create-set-attach dance: one -A.
        self.assertNotIn("has-session", out)
        self.assertIn("exec tmux -u new-session -A -s shell'", out)

    def test_waypipe_and_ecf_share_one_script(self):
        cfg = sshtsf.load_config()
        cfg["hosts"]["devbox"]["sessions"]["api"]["waypipe"] = True
        sshtsf.save_config(cfg)
        with mock.patch.object(sshtsf, "ecf_local_socket", return_value="/x/server"), \
             mock.patch.object(sshtsf, "waypipe_local_display",
                               return_value=("/run/user/1000/wayland-0", "")), \
             mock.patch.object(sshtsf, "waypipe_remote_missing", return_value=""):
            rc, out, _ = run_capture(["devbox", "api", "--dry-run"])
        self.assertEqual(rc, 0)
        cmd = out.strip().splitlines()[1]
        self.assertTrue(cmd.startswith("waypipe ssh -o ExitOnForwardFailure=yes -R "))
        # The shell already holds waypipe's WAYLAND_DISPLAY; only the target
        # needs exporting into a fallback shell.
        self.assertIn(no_tmux("src/api", (("EMACS_REMOTE_TARGET", "devbox"),)), cmd)
        self.assertNotIn("export WAYLAND_DISPLAY", cmd)
        self.assertIn(
            "|| exit 1; "
            "tmux set-environment -t =api WAYLAND_DISPLAY \"$WAYLAND_DISPLAY\"; "
            "tmux set-environment -t =api EMACS_REMOTE_TARGET devbox; "
            "exec tmux -u attach-session -t =api", cmd)

    def test_ecf_flag_without_local_server_warns_and_connects(self):
        with mock.patch.object(sshtsf, "ecf_local_socket", return_value=None):
            rc, out, err = run_capture(["-e", "devbox", "web", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertNotIn("-R", out)
        self.assertIn("no local Emacs server socket (start Emacs first)", err)
        self.assertIn("connecting without the forward", err)

    def test_waypipe_flag_without_local_display_warns_and_connects(self):
        with mock.patch.object(sshtsf, "waypipe_local_display",
                               return_value=(None, "waypipe is not on PATH")), \
             mock.patch.object(sshtsf, "waypipe_remote_missing") as probe:
            rc, out, err = run_capture(["-w", "devbox", "web", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertTrue(out.strip().startswith("ssh "))
        self.assertIn("no local Wayland display (waypipe is not on PATH)", err)
        self.assertIn("connecting without it", err)
        # No point asking the remote once this end cannot do it.
        probe.assert_not_called()

    def test_waypipe_flag_missing_on_remote_warns_and_connects(self):
        with mock.patch.object(sshtsf, "waypipe_local_display",
                               return_value=("/run/user/1000/wayland-0", "")), \
             mock.patch.object(sshtsf, "waypipe_remote_missing",
                               return_value="no waypipe on the non-interactive PATH of devbox"):
            rc, out, err = run_capture(["-w", "devbox", "web", "--dry-run"])
        self.assertEqual(rc, 0)
        cmd = out.strip()
        self.assertTrue(cmd.startswith("ssh "))
        self.assertNotIn("waypipe", cmd)
        self.assertNotIn("WAYLAND_DISPLAY", cmd)
        self.assertIn("no waypipe on the non-interactive PATH of devbox; "
                      "connecting without Wayland", err)

    def test_ecf_default_without_local_server_connects_plain(self):
        with mock.patch.object(sshtsf, "ecf_local_socket", return_value=None):
            rc, out, err = run_capture(["devbox", "api", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertNotIn("-R", out)
        self.assertIn("connecting without the forward", err)

    def test_no_ecf_flag_overrides_session(self):
        with mock.patch.object(sshtsf, "ecf_local_socket",
                               return_value="/x/server") as probe:
            rc, out, _ = run_capture(["-E", "devbox", "api", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertNotIn("-R", out)
        probe.assert_not_called()

    def test_ecf_port_relays_through_socat(self):
        cfg = sshtsf.load_config()
        cfg["hosts"]["build"].setdefault("sessions", {})["s"] = {"ecf": True}
        sshtsf.save_config(cfg)
        with mock.patch.object(sshtsf, "ecf_local_socket", return_value="/x/server"):
            rc, out, _ = run_capture(["build", "s", "--dry-run"])
        self.assertEqual(rc, 0)
        cmd = out.strip().splitlines()[1]
        self.assertIn("-R 41234:/x/server", cmd)
        self.assertIn("socat UNIX-LISTEN:/tmp/emacs-remote-socket,fork TCP:127.0.0.1:41234", cmd)
        self.assertIn("|| tmux -u new-session -d -s s || exit 1; ", cmd)
        self.assertIn("EMACS_REMOTE_TARGET build.internal; ", cmd)
        # No exec on the attach, or the kill after it would never run.
        self.assertIn("; tmux -u attach-session -t =s; "
                      "[ -z \"$SOCAT_PID\" ] || kill $SOCAT_PID", cmd)
        # A remote without socat is warned, not refused: the relay is skipped
        # and the attach still happens.
        self.assertIn("if command -v socat >/dev/null; then socat ", cmd)
        # The tmux check comes first, so a fallback shell never leaves a relay
        # running behind it.
        self.assertLess(cmd.index("command -v tmux"), cmd.index("command -v socat"))
        self.assertIn("else echo \"sshtsf: no socat on the remote; "
                      "the Emacs relay is off\" >&2; fi; ", cmd)

    def test_unknown_host_lists_registered(self):
        rc, _, err = run_capture(["nowhere", "--dry-run"])
        self.assertEqual(rc, 1)
        self.assertIn("unknown host or alias: nowhere", err)
        self.assertIn("devbox  (alias c, default)", err)

    def test_host_picker_shows_the_target(self):
        cfg = sshtsf.load_config()
        cfg["hosts"]["devbox"]["target"] = "me@devbox.local"
        sshtsf.save_config(cfg)
        with mock.patch.object(sshtsf, "pick", return_value=None) as pick:
            run_capture(["--dry-run"])
        items = pick.call_args.args[0]
        self.assertEqual(items[0], "devbox  (alias c, -> me@devbox.local, default)")
        self.assertEqual(items[-1], sshtsf.NEW_HOST)

    def test_live_header_shows_the_target(self):
        with mock.patch.object(sshtsf, "live_sessions", return_value=([], "")):
            rc, out, _ = run_capture(["live"])
        self.assertEqual(rc, 0)
        self.assertIn("build  (-> build.internal):\n", out)
        self.assertIn("devbox:\n", out)

    def test_list_marks_last_used(self):
        rc, out, _ = run_capture(["list"])
        self.assertEqual(rc, 0)
        self.assertIn("devbox  (alias c, default)", out)
        self.assertIn("web  (devweb)  ~/src/webapp  -> make dev *", out)
        self.assertIn("api  ~/src/api  +ecf", out)
        self.assertIn("build  (-> build.internal)\n    (no sessions)", out)


class TestRemoteSh(unittest.TestCase):
    def test_path_goes_first(self):
        self.assertEqual(sshtsf.remote_sh("tmux -V"),
                         ["sh", "-c", REMOTE_PATH_LINE + "tmux -V"])

    def test_env_knob_replaces_the_list(self):
        with mock.patch.object(sshtsf, "REMOTE_PATH", "/opt/tmux/bin"):
            self.assertEqual(sshtsf.remote_sh("tmux -V")[2],
                             'PATH="/opt/tmux/bin:$PATH"; tmux -V')

    def test_live_sessions_probe_uses_it(self):
        proc = mock.Mock(returncode=0, stdout="web\t2w\tdetached\n", stderr="")
        with mock.patch.object(sshtsf.subprocess, "run", return_value=proc) as run:
            sessions, why = sshtsf.live_sessions("devbox")
        self.assertEqual((sessions, why), (["web\t2w\tdetached"], ""))
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["ssh", "devbox", "sh", "-c"])
        # Quoted once for the remote login shell; sh then sees the script.
        script = shlex.split(argv[4])[0]
        self.assertTrue(script.startswith(REMOTE_PATH_LINE + "tmux list-sessions -F "))

    def test_remote_tmux_probe_uses_it(self):
        proc = mock.Mock(returncode=0, stdout="/opt/homebrew/bin/tmux\ntmux 3.5a\n")
        with mock.patch.object(sshtsf.subprocess, "run", return_value=proc) as run:
            self.assertEqual(sshtsf.remote_tmux("mac"), "/opt/homebrew/bin/tmux (tmux 3.5a)")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:6], ["ssh", "-o", "BatchMode=yes", "mac", "sh", "-c"])
        self.assertIn("command -v tmux && tmux -V", argv[6])


class TestWaypipeRemoteProbe(unittest.TestCase):
    def probe(self, returncode, opts=()):
        proc = mock.Mock(returncode=returncode)
        with mock.patch.object(sshtsf, "WAYPIPE_OPTS", list(opts)), \
             mock.patch.object(sshtsf.subprocess, "run", return_value=proc) as run:
            why = sshtsf.waypipe_remote_missing("devbox")
        return why, run.call_args.args[0]

    def test_present(self):
        why, argv = self.probe(0)
        self.assertEqual(why, "")
        self.assertEqual(argv, ["ssh", "-o", "BatchMode=yes", "devbox", "command -v waypipe"])

    def test_missing(self):
        why, _ = self.probe(1)
        self.assertEqual(why, "no waypipe on the non-interactive PATH of devbox")

    def test_ssh_failure_is_not_an_answer(self):
        why, _ = self.probe(255)
        self.assertEqual(why, "")

    def test_timeout_is_not_an_answer(self):
        with mock.patch.object(sshtsf.subprocess, "run",
                               side_effect=sshtsf.subprocess.TimeoutExpired("ssh", 20)):
            self.assertEqual(sshtsf.waypipe_remote_missing("devbox"), "")

    def test_remote_bin_is_probed_instead_of_path(self):
        why, argv = self.probe(1, ["--compress", "zstd", "--remote-bin", "/opt/wp/bin/waypipe"])
        self.assertEqual(argv[-1], "test -x /opt/wp/bin/waypipe")
        self.assertEqual(why, "/opt/wp/bin/waypipe is not executable on devbox")

    def test_remote_bin_with_equals(self):
        _, argv = self.probe(0, ["--remote-bin=/opt/wp/bin/waypipe"])
        self.assertEqual(argv[-1], "test -x /opt/wp/bin/waypipe")


class TestSubcommands(ConfigDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        sshtsf.save_config(SAMPLE)

    def test_set_and_clear_fields(self):
        rc, out, _ = run_capture(["set", "devbox", "web", "command", "npm start"])
        self.assertEqual(rc, 0)
        self.assertEqual(sshtsf.load_config()["hosts"]["devbox"]["sessions"]["web"]["command"],
                         "npm start")
        rc, _, _ = run_capture(["set", "devbox", "ecf", "yes"])
        self.assertEqual(rc, 0)
        self.assertIs(sshtsf.load_config()["hosts"]["devbox"]["ecf"], True)
        rc, _, err = run_capture(["set", "devbox", "ecf", "maybe"])
        self.assertEqual(rc, 1)
        self.assertIn("takes", err)
        rc, _, _ = run_capture(["set", "devbox", "web", "command", ""])
        self.assertEqual(rc, 0)
        self.assertNotIn("command",
                         sshtsf.load_config()["hosts"]["devbox"]["sessions"]["web"])

    def test_rm_session_and_host(self):
        rc, _, _ = run_capture(["rm", "devbox", "web"])
        self.assertEqual(rc, 0)
        cfg = sshtsf.load_config()
        self.assertNotIn("web", cfg["hosts"]["devbox"]["sessions"])
        self.assertNotIn("last", cfg)  # last pointed at the removed session
        rc, _, err = run_capture(["rm", "devbox"])
        self.assertEqual(rc, 1)
        self.assertIn("still has", err)
        # The flag trails: argparse takes no option between two positionals here.
        rc, _, _ = run_capture(["rm", "devbox", "-f"])
        self.assertEqual(rc, 0)
        cfg = sshtsf.load_config()
        self.assertNotIn("devbox", cfg["hosts"])
        self.assertNotIn("default_host", cfg)

    def test_default(self):
        rc, _, _ = run_capture(["default", "build"])
        self.assertEqual(rc, 0)
        self.assertEqual(sshtsf.load_config()["default_host"], "build")

    def test_remote_dry_run_uses_mirrored_path(self):
        with mock.patch.object(sshtsf, "home_relative_repo",
                               return_value=("src/mytool", "/home/me/src/mytool")), \
             mock.patch.object(sshtsf, "git_remote_url", return_value=None), \
             mock.patch.object(sshtsf, "valid_remote_name", return_value=True):
            rc, out, _ = run_capture(["remote", "c", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "git remote add c devbox:src/mytool")


if __name__ == "__main__":
    unittest.main()
