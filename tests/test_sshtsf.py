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
import textwrap
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


# How every probe dials: bounded to reach the host (see logins.probe_ssh).
PROBE = ["ssh", "-o", "ConnectTimeout=10"]

REMOTE_PATH_LINE = ('PATH="/opt/homebrew/bin:/usr/local/bin:'
                    '/home/linuxbrew/.linuxbrew/bin:$HOME/.local/bin:$PATH"; ')


def shell_instead(reason: str, folder: str = "",
                  env: tuple[tuple[str, str], ...] = (), relay: bool = False) -> str:
    """The block that gives up on tmux and gives a login shell instead."""
    return ('{ echo "sshtsf: %s; a plain shell instead" >&2; ' % reason
            + ('[ -z "$SOCAT_PID" ] || kill $SOCAT_PID 2>/dev/null; ' if relay else "")
            + (f"cd {folder} 2>/dev/null; " if folder else "")
            + "".join("export %s=%s; " % pair for pair in env)
            + 'exec "${SHELL:-sh}" -l; }')


def attach(session: str, folder: str = "", env: tuple[tuple[str, str], ...] = (),
           relay: bool = False) -> str:
    return "tmux -u attach-session -t =%s || %s" % (
        session, shell_instead("could not attach to the session", folder, env, relay))


def no_tmux(folder: str = "", env: tuple[tuple[str, str], ...] = ()) -> str:
    """The remote script's opening: the PATH, then what happens without tmux."""
    return (REMOTE_PATH_LINE + "command -v tmux >/dev/null || "
            + shell_instead("no tmux on the remote", folder, env) + "; ")


def create(session: str, folder: str = "", env: tuple[tuple[str, str], ...] = (),
           command: str = "", relay: bool = False) -> str:
    """The create step: has-session, else new-session, else a shell."""
    plain = "tmux -u new-session -d %s-s %s" % ("-c %s " % folder if folder else "", session)
    make = plain
    if command:
        make = ('%s %s || { echo "sshtsf: the session command failed to start; '
                'a session without it instead" >&2; %s; }' % (plain, command, plain))
    return ("tmux has-session -t =%s 2>/dev/null || %s || %s; "
            % (session, make,
               shell_instead("could not create the session", folder, env, relay)))


class Terminal(io.StringIO):
    """A captured stdout that says it is a terminal."""

    def isatty(self) -> bool:
        return True


def run_capture(argv: list[str], tty: bool = False) -> tuple[int, str, str]:
    out, err = (Terminal() if tty else io.StringIO()), io.StringIO()
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
        # The login a dry run guesses for the remote socket, when the target
        # names none: pinned, so expectations do not depend on who runs this.
        patcher = mock.patch("getpass.getuser", return_value="me")
        patcher.start()
        self.addCleanup(patcher.stop)
        # No ssh-lists file unless a test writes one: the real one would
        # otherwise be offered when a host is registered.
        patcher = mock.patch.dict(os.environ, {"SSH_LISTS_FILE": "/nonexistent/ssh-lists.toml"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg_path = cfg_path

    def write_lists(self, text: str) -> None:
        path = os.path.join(self.tmp.name, "ssh-lists.toml")
        with open(path, "w") as fh:
            fh.write(text)
        os.environ["SSH_LISTS_FILE"] = path


class TestHelp(unittest.TestCase):
    def test_help_exits_zero_and_uses_generic_names(self):
        rc, out, _ = run_capture(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("devbox", out)
        self.assertIn("sshtsf -c devbox web", out)
        self.assertIn("forwarding the local Emacs socket", out)

    def test_contradictory_flags_are_rejected(self):
        rc, _, err = run_capture(["-e", "-E", "devbox"])
        self.assertEqual(rc, 2)
        self.assertIn("contradictory", err)

    def test_one_action_at_a_time(self):
        rc, _, err = run_capture(["-l", "-L"])
        self.assertEqual(rc, 2)
        self.assertIn("one of -l, -L at a time", err)

    def test_word_counts(self):
        rc, _, err = run_capture(["-n"])
        self.assertEqual(rc, 2)
        self.assertIn("-n needs a host", err)
        rc, _, err = run_capture(["-l", "devbox", "web"])
        self.assertEqual(rc, 2)
        self.assertIn("at most 1 word", err)
        rc, _, err = run_capture(["a", "b", "c"])
        self.assertEqual(rc, 2)
        self.assertIn("at most 2 word", err)


class TestExampleConfig(ConfigDirMixin, unittest.TestCase):
    """`sshtsf -h' carries the config layout, and so does the example file,
    for a reader without the help at hand; the two stay one text."""

    EXAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "examples", "sshtsf-config.toml")

    def test_help_and_example_agree(self):
        doc = sshtsf.__doc__
        in_help = textwrap.dedent(doc[doc.index("    default_host"):]).strip()
        with open(self.EXAMPLE, encoding="utf-8") as fh:
            example = fh.read()
        self.assertEqual(in_help, example[example.index("default_host"):].strip())

    def test_example_loads(self):
        os.makedirs(os.path.dirname(self.cfg_path))
        with open(self.EXAMPLE, encoding="utf-8") as src, open(self.cfg_path, "w") as dst:
            dst.write(src.read())
        cfg = sshtsf.load_config()
        self.assertEqual(sshtsf.word_owners(cfg, "devweb"), [("devbox", "web")])
        self.assertEqual(cfg["hosts"]["devbox"]["ecf_port"], 41234)


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


def scripted(answers: dict[str, str | list[str]]):
    """An `ask' that answers by prompt prefix, and fails on a prompt it lacks.
    A list answers the same prompt asked again, in turn."""
    answers = {prefix: list(reply) if isinstance(reply, list) else reply
               for prefix, reply in answers.items()}

    def fake_ask(prompt, default=""):
        for prefix, reply in answers.items():
            if prompt.strip().startswith(prefix):
                if isinstance(reply, list):
                    reply = reply.pop(0)
                return reply or default
        raise AssertionError(f"unexpected prompt: {prompt!r}")
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
             mock.patch.object(sshtsf, "choose",
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
        self.assertNotIn("ssh-lists", err)
        self.assertIn("`sshtsf -c nowhere` registers it", err)

    def test_unknown_host_hint_shows_the_lists_logins_first(self):
        self.write_lists('logins = ["alice@newbox", "devbox", "other"]\n')
        with mock.patch.object(sshtsf, "ssh_known_hosts", return_value=["devbox", "other"]):
            rc, _, err = run_capture(["nowhere", "--dry-run"])
        self.assertEqual(rc, 1)
        # A login is offered as is; a machine the lists name is not repeated
        # from known_hosts, and one an entry dials is in neither list.
        self.assertIn("in ssh-lists:\n          alice@newbox, other\n", err)
        self.assertNotIn("known_hosts", err)

    def test_lists_logins_are_offered_first_and_are_the_destination(self):
        self.write_lists('logins = ["alice@newbox", { login = "build.example.com",'
                         ' commands = ["cd src"] }, "alice@newbox"]\n')
        # No "user on it": the login names one. No "name" typed: the default
        # is the login's machine part.
        answers = {"name for this [user@]host": "", "ssh destination": "", "alias": "",
                   "forward the local Emacs socket": "", "forward Wayland": ""}
        with mock.patch.object(sshtsf, "ssh_known_hosts",
                               return_value=["newbox", "other"]), \
             mock.patch.object(sshtsf, "choose", return_value="alice@newbox") as pick, \
             mock.patch.object(sshtsf, "ask", side_effect=scripted(answers)), \
             mock.patch.object(sshtsf, "route_add_session", return_value=0):
            rc = sshtsf.route_add_host(sshtsf.load_config())
        self.assertEqual(rc, 0)
        self.assertEqual(pick.call_args.args[0],
                         ["alice@newbox", "build.example.com", "other"])
        self.assertEqual(sshtsf.load_config()["hosts"]["newbox"],
                         {"target": "alice@newbox"})

    def test_a_listed_login_an_entry_dials_is_noted(self):
        self.write_lists('logins = ["me@devbox", "c"]\n')
        with mock.patch.object(sshtsf, "ssh_known_hosts", return_value=[]):
            got = sshtsf.host_candidates(sshtsf.load_config())
        self.assertEqual(got, [("me@devbox", "registered as devbox", "ssh-lists"),
                               ("c", "registered as devbox", "ssh-lists")])

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
                              "relay it": "41234", "forward Wayland": ""})
        self.assertEqual(hcfg, {"target": "me@mac.local", "alias": "m", "ecf": True,
                                "ecf_port": 41234})

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
             mock.patch.object(sshtsf, "choose", return_value=picked) as pick:
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

    def test_leaving_the_picker_keeps_the_folder(self):
        # It used to return None, which ended the whole walk.
        got, _, _, pick = self.prompt("?", picked=None)
        self.assertEqual(got, "")
        self.assertEqual(pick.call_args.args[2], "remote folder (abort keeps none)")
        got, _, _, pick = self.prompt("?", current="src/old", picked=None)
        self.assertEqual(got, "src/old")
        self.assertEqual(pick.call_args.args[2], "remote folder (abort keeps src/old)")

    def test_a_failed_listing_keeps_the_current_on_a_blank(self):
        # "?" first, then a blank at the prompt that replaces the listing:
        # it used to have no default, so the blank cleared the folder.
        with mock.patch.object(sshtsf, "ask", side_effect=scripted({"folder": ["?", ""]})) as ask, \
             mock.patch.object(sshtsf, "remote_dirs", return_value=[]):
            got = sshtsf.prompt_folder("devbox", "src/old")
        self.assertEqual(got, "src/old")
        self.assertEqual(ask.call_args.args, ("  folder relative to ~ (- for none)", "src/old"))
        with mock.patch.object(sshtsf, "ask", side_effect=scripted({"folder": ["?", "-"]})), \
             mock.patch.object(sshtsf, "remote_dirs", return_value=[]):
            self.assertEqual(sshtsf.prompt_folder("devbox", "src/old"), "")


class TestEdit(ConfigDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        sshtsf.save_config(SAMPLE)

    def test_visual_wins_and_is_split_like_a_shell(self):
        with mock.patch.dict(os.environ, {"VISUAL": "emacsclient -t", "EDITOR": "nano"}):
            rc, out, _ = run_capture(["--edit", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "emacsclient -t " + self.cfg_path)

    def test_editor_then_vi(self):
        with mock.patch.dict(os.environ, {"VISUAL": "", "EDITOR": "nano"}):
            _, out, _ = run_capture(["--edit", "--dry-run"])
        self.assertEqual(out.strip(), "nano " + self.cfg_path)
        with mock.patch.dict(os.environ, {"VISUAL": "", "EDITOR": ""}):
            _, out, _ = run_capture(["--edit", "--dry-run"])
        self.assertEqual(out.strip(), "vi " + self.cfg_path)

    def test_missing_config_is_created_first(self):
        os.remove(self.cfg_path)
        with mock.patch.dict(os.environ, {"VISUAL": "ed"}), \
             mock.patch.object(sshtsf.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as run:
            rc, _, _ = run_capture(["--edit"])
        self.assertEqual(rc, 0)
        run.assert_called_once_with(["ed", self.cfg_path])
        self.assertEqual(sshtsf.load_config(), {"hosts": {}})

    def test_dry_run_writes_nothing(self):
        # It used to create the missing config before printing the command.
        os.remove(self.cfg_path)
        with mock.patch.dict(os.environ, {"VISUAL": "ed"}):
            rc, out, _ = run_capture(["--edit", "--dry-run"])
        self.assertEqual((rc, out.strip()), (0, "ed " + self.cfg_path))
        self.assertFalse(os.path.exists(self.cfg_path))

    def test_runs_editor_and_reports_a_slip(self):
        def scribble(argv, **kw):
            with open(argv[-1], "w") as fh:
                fh.write("[hosts.devbox\n")
            return mock.Mock(returncode=0)

        with mock.patch.dict(os.environ, {"VISUAL": "ed"}), \
             mock.patch.object(sshtsf.subprocess, "run", side_effect=scribble) as run:
            rc, _, err = run_capture(["--edit"])
        self.assertEqual(rc, 1)
        self.assertEqual(run.call_args.args[0], ["ed", self.cfg_path])
        self.assertIn("not valid TOML", err)

    def test_editor_failure_is_passed_on(self):
        with mock.patch.dict(os.environ, {"VISUAL": "ed"}), \
             mock.patch.object(sshtsf.subprocess, "run",
                               return_value=mock.Mock(returncode=3)):
            rc, _, err = run_capture(["--edit"])
        self.assertEqual(rc, 3)
        self.assertIn("ed exited 3", err)

    def test_works_on_a_config_that_will_not_parse(self):
        with open(self.cfg_path, "w") as fh:
            fh.write("[hosts.devbox\n")
        # Every other action stops at the parse error; --edit is the way past it.
        rc, _, err = run_capture(["-l"])
        self.assertEqual(rc, 1)
        self.assertIn("not valid TOML", err)

        def repair(argv, **kw):
            sshtsf.save_config(SAMPLE)
            return mock.Mock(returncode=0)

        with mock.patch.dict(os.environ, {"VISUAL": "ed"}), \
             mock.patch.object(sshtsf.subprocess, "run", side_effect=repair):
            rc, _, err = run_capture(["--edit"])
        self.assertEqual(rc, 0)
        # Told why the editor opened, before it did.
        self.assertIn("not valid TOML", err)
        self.assertEqual(run_capture(["-l"])[0], 0)


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

    def test_word_owners_are_every_entry_a_word_names(self):
        self.assertEqual(sshtsf.word_owners(SAMPLE, "devweb"), [("devbox", "web")])
        self.assertEqual(sshtsf.word_owners(SAMPLE, "c"), [("devbox", "")])
        self.assertEqual(sshtsf.word_owners(SAMPLE, "build"), [("build", "")])
        # A session's name is not a word of its own; only its alias is.
        self.assertEqual(sshtsf.word_owners(SAMPLE, "web"), [])
        cfg = {"hosts": {"alpha": {"sessions": {"main": {"alias": "main"}}},
                         "beta": {"alias": "alpha",
                                  "sessions": {"main": {"alias": "main"}}}}}
        self.assertEqual(sshtsf.word_owners(cfg, "main"), [("alpha", "main"), ("beta", "main")])
        self.assertEqual(sshtsf.word_owners(cfg, "alpha"), [("alpha", ""), ("beta", "")])
        self.assertEqual(sshtsf.word_clash(cfg, "main", ("alpha", "main")), "beta+main")
        self.assertEqual(sshtsf.word_clash(SAMPLE, "c", ("devbox", "")), "")

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
            + create("web", "src/webapp", command="make dev")
            + attach("web", "src/webapp") + "'")

    def test_pair_alias_and_host_alias(self):
        rc, out, _ = run_capture(["devweb", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("-s web make dev", out)
        rc, out, _ = run_capture(["c", "shell", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "ssh -t devbox sh -c '" + no_tmux()
                         + create("shell") + attach("shell") + "'")

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
        self.assertEqual(lines[0], "ssh -o ConnectTimeout=10 devbox sh -c '" + REMOTE_PATH_LINE
                         + "p=/tmp/emacs-remote-socket-$(id -un); rm -f \"$p\" && echo \"$p\"'")
        self.assertEqual(
            lines[1],
            "ssh -R /tmp/emacs-remote-socket-me:/run/user/1000/emacs/server "
            "-t devbox sh -c '"
            + no_tmux("src/api", (("EMACS_REMOTE_TARGET", "devbox"),))
            + create("api", "src/api", (("EMACS_REMOTE_TARGET", "devbox"),))
            +             "tmux set-environment -t =api EMACS_REMOTE_TARGET devbox; "
            + attach("api", "src/api", (("EMACS_REMOTE_TARGET", "devbox"),)) + "'")

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
        self.assertNotIn("set-environment", out)
        self.assertIn("; tmux -u attach-session -t =shell || ", out)

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
        self.assertTrue(cmd.startswith("waypipe ssh -R "))
        # The shell already holds waypipe's WAYLAND_DISPLAY; only the target
        # needs exporting into a fallback shell.
        self.assertIn(no_tmux("src/api", (("EMACS_REMOTE_TARGET", "devbox"),)), cmd)
        self.assertNotIn("export WAYLAND_DISPLAY", cmd)
        self.assertIn(
            "; tmux set-environment -t =api WAYLAND_DISPLAY \"$WAYLAND_DISPLAY\"; "
            "tmux set-environment -t =api EMACS_REMOTE_TARGET devbox; "
            "tmux -u attach-session -t =api || ", cmd)

    def test_cleanup_failure_drops_the_forward_and_connects(self):
        # rm -f cannot remove another user's socket in a sticky /tmp, and the
        # bind would fail on the same file: warn, and connect plain.
        rm = mock.Mock(returncode=1, stdout="",
                       stderr="rm: cannot remove '/tmp/emacs-remote-socket-root': "
                              "Operation not permitted\n")
        with mock.patch.object(sshtsf, "ecf_local_socket", return_value="/x/server"), \
             mock.patch.object(sshtsf.subprocess, "run", return_value=rm) as run, \
             mock.patch.object(sshtsf.os, "execvp") as execvp:
            _, _, err = run_capture(["devbox", "api"])
        self.assertEqual(run.call_args.args[0][:4], PROBE + ["devbox"])
        self.assertIn("could not clear the Emacs socket on devbox "
                      "(rm: cannot remove '/tmp/emacs-remote-socket-root': Operation not "
                      "permitted); connecting without the forward", err)
        argv = execvp.call_args.args[1]
        self.assertNotIn("-R", argv)
        self.assertNotIn("EMACS_REMOTE_TARGET", " ".join(argv))
        self.assertIn("devbox -> api\n", err)

    def test_cleanup_success_keeps_the_forward(self):
        with mock.patch.object(sshtsf, "ecf_local_socket", return_value="/x/server"), \
             mock.patch.object(sshtsf.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stderr="",
                                                      stdout="/tmp/emacs-remote-socket-root\n")), \
             mock.patch.object(sshtsf.os, "execvp") as execvp:
            _, _, err = run_capture(["devbox", "api"])
        # The path the remote answered, for the login it actually gave.
        argv = execvp.call_args.args[1]
        self.assertEqual(argv[:3], ["ssh", "-R", "/tmp/emacs-remote-socket-root:/x/server"])
        self.assertIn("EMACS_REMOTE_TARGET devbox", " ".join(argv))
        self.assertIn("devbox -> api +ecf\n", err)

    def test_cleanup_that_answers_no_path_drops_the_forward(self):
        with mock.patch.object(sshtsf, "ecf_local_socket", return_value="/x/server"), \
             mock.patch.object(sshtsf.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stderr="", stdout="")), \
             mock.patch.object(sshtsf.os, "execvp") as execvp:
            _, _, err = run_capture(["devbox", "api"])
        self.assertNotIn("-R", execvp.call_args.args[1])
        self.assertIn("could not clear the Emacs socket on devbox (exit 0)", err)

    def test_dry_run_guesses_the_login_from_the_target(self):
        cfg = sshtsf.load_config()
        cfg["hosts"]["devbox"]["target"] = "root@devbox"
        sshtsf.save_config(cfg)
        with mock.patch.object(sshtsf, "ecf_local_socket", return_value="/x/server"):
            _, out, _ = run_capture(["devbox", "api", "--dry-run"])
        self.assertIn(" -R /tmp/emacs-remote-socket-root:/x/server ", out)

    def test_ecf_flag_without_local_server_warns_and_connects(self):
        with mock.patch.object(sshtsf, "ecf_local_socket", return_value=None):
            rc, out, err = run_capture(["-e", "devbox", "web", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertNotIn("-R", out)
        self.assertIn("no local Emacs server socket (start Emacs first)", err)
        self.assertIn("connecting without the forward", err)

    def waypipe_on(self):
        cfg = sshtsf.load_config()
        cfg["hosts"]["devbox"]["sessions"]["api"]["waypipe"] = True
        sshtsf.save_config(cfg)

    def connect_for_real(self, argv: list[str], tty: bool):
        """Run up to the exec with the forwards' probes answered, and say
        what reached stdout."""
        with mock.patch.object(sshtsf, "ecf_local_socket", return_value=None), \
             mock.patch.object(sshtsf, "waypipe_local_display",
                               return_value=("/run/user/1000/wayland-0", "")), \
             mock.patch.object(sshtsf, "waypipe_remote_missing", return_value=""), \
             mock.patch.object(sshtsf.os, "execvp") as execvp:
            rc, out, err = run_capture(argv, tty=tty)
        return rc, out, err, execvp

    def test_waypipe_session_names_the_tab(self):
        # Konsole sees waypipe, not the ssh behind it, so the tab is named
        # here: its own OSC 30, then the window title other terminals show.
        self.waypipe_on()
        _, out, err, execvp = self.connect_for_real(["devbox", "api"], tty=True)
        self.assertEqual(execvp.call_args.args[0], "waypipe")
        self.assertEqual(out, "\033]30;devbox:api\007\033]0;devbox:api\007")
        self.assertIn("devbox -> api +waypipe\n", err)

    def test_plain_session_leaves_the_tab_to_the_terminal(self):
        _, out, _, execvp = self.connect_for_real(["devbox", "api"], tty=True)
        self.assertEqual(execvp.call_args.args[0], "ssh")
        self.assertEqual(out, "")

    def test_tab_title_needs_a_terminal(self):
        self.waypipe_on()
        _, out, _, execvp = self.connect_for_real(["devbox", "api"], tty=False)
        self.assertEqual(execvp.call_args.args[0], "waypipe")
        self.assertEqual(out, "")

    def test_dry_run_prints_the_command_and_no_title(self):
        self.waypipe_on()
        _, out, _, execvp = self.connect_for_real(["devbox", "api", "--dry-run"], tty=True)
        execvp.assert_not_called()
        self.assertEqual(len(out.splitlines()), 1)
        self.assertTrue(out.startswith("waypipe ssh -t devbox "))
        self.assertNotIn("\033", out)

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
        self.assertIn("socat UNIX-LISTEN:/tmp/emacs-remote-socket-me,fork TCP:127.0.0.1:41234", cmd)
        self.assertIn(create("s", env=(("EMACS_REMOTE_TARGET", "build.internal"),),
                             relay=True), cmd)
        self.assertIn("EMACS_REMOTE_TARGET build.internal; ", cmd)
        # No exec on the attach, or the kill after it would never run.
        self.assertIn("; " + attach("s", env=(("EMACS_REMOTE_TARGET", "build.internal"),),
                                   relay=True)
                      + "; [ -z \"$SOCAT_PID\" ] || kill $SOCAT_PID", cmd)
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

    def test_unknown_host_is_refused_not_registered(self):
        # The README once promised `sshtsf newbox api` would register newbox;
        # only -c does. Any prompt here fails the scripted ask.
        with mock.patch.object(sshtsf, "ask", side_effect=scripted({})), \
             mock.patch.object(sshtsf, "ssh_known_hosts", return_value=[]):
            rc, _, err = run_capture(["newbox", "api", "--dry-run"])
        self.assertEqual(rc, 1)
        self.assertIn("unknown host: newbox\n", err)
        self.assertIn("`sshtsf -c newbox` registers it", err)
        self.assertNotIn("newbox", sshtsf.load_config()["hosts"])

    def test_one_wording_and_one_suggestion_for_an_unknown_host(self):
        # Every connect path words it alike; the one-word form also says
        # "or alias", and adds -l for the session aliases.
        with mock.patch.object(sshtsf, "ssh_known_hosts", return_value=[]):
            for argv in (["nowhere", "web"], ["-n", "nowhere"]):
                rc, _, err = run_capture(argv + ["--dry-run"])
                self.assertEqual(rc, 1)
                self.assertTrue(err.startswith("sshtsf: unknown host: nowhere\n"), err)
                self.assertIn("\n        `sshtsf -c nowhere` registers it\n", err)
            rc, _, err = run_capture(["nowhere", "--dry-run"])
            self.assertTrue(err.startswith("sshtsf: unknown host or alias: nowhere\n"), err)
            self.assertIn("`sshtsf -c nowhere` registers it; `sshtsf -l` shows the sessions too", err)
            # Nothing registered: only the hint's own "run `sshtsf`". This
            # used to come with a competing `sshtsf -c` line for two words.
            os.remove(self.cfg_path)
            for argv in (["nowhere"], ["nowhere", "web"]):
                rc, _, err = run_capture(argv + ["--dry-run"])
                self.assertEqual(rc, 1)
                self.assertIn("nothing registered yet; run `sshtsf` to add a host", err)
                self.assertNotIn("sshtsf -c", err)

    def test_unknown_session_on_a_known_host_is_registered(self):
        with mock.patch.object(sshtsf, "route_add_session", return_value=0) as add:
            rc, _, err = run_capture(["c", "api2", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("no session api2 on devbox", err)
        self.assertEqual(add.call_args.args[:3], (mock.ANY, "devbox", "api2"))

    def test_a_word_naming_two_entries_is_reported_not_guessed(self):
        cfg = sshtsf.load_config()
        cfg["hosts"]["build"].setdefault("sessions", {})["main"] = {"alias": "main"}
        cfg["hosts"]["devbox"]["sessions"]["main"] = {"alias": "main"}
        # A session alias that is also a host's name.
        cfg["hosts"]["devbox"]["sessions"]["b"] = {"alias": "build"}
        sshtsf.save_config(cfg)
        with mock.patch.object(sshtsf, "connect") as connect, \
             mock.patch.object(sshtsf, "route_pick_session") as pick_session:
            rc, _, err = run_capture(["main", "--dry-run"])
            self.assertEqual(rc, 1)
            self.assertIn("main is ambiguous: it names build+main, devbox+main", err)
            rc, _, err = run_capture(["build", "--dry-run"])
            self.assertEqual(rc, 1)
            self.assertIn("build is ambiguous: it names host build, devbox+b", err)
        connect.assert_not_called()
        pick_session.assert_not_called()

    def test_host_picker_shows_the_target(self):
        cfg = sshtsf.load_config()
        cfg["hosts"]["devbox"]["target"] = "me@devbox.local"
        sshtsf.save_config(cfg)
        with mock.patch.object(sshtsf, "choose", return_value=None) as pick:
            run_capture(["--dry-run"])
        items = pick.call_args.args[0]
        self.assertEqual(items[0], "devbox  (alias c, -> me@devbox.local, default)")
        self.assertEqual(items[-1], sshtsf.NEW_HOST)

    def test_live_header_shows_the_target(self):
        with mock.patch.object(sshtsf, "live_sessions", return_value=([], "")):
            rc, out, _ = run_capture(["-L"])
        self.assertEqual(rc, 0)
        self.assertIn("build  (-> build.internal):\n", out)
        self.assertIn("devbox:\n", out)

    def test_list_marks_last_used(self):
        rc, out, _ = run_capture(["-l"])
        self.assertEqual(rc, 0)
        self.assertIn("devbox  (alias c, default)", out)
        self.assertIn("web  (devweb)  ~/src/webapp  -> make dev *", out)
        self.assertIn("api  ~/src/api  +ecf", out)
        self.assertIn("build  (-> build.internal)\n    (no sessions)", out)

    def test_list_and_live_take_a_host(self):
        rc, out, _ = run_capture(["-l", "c"])
        self.assertEqual(rc, 0)
        self.assertNotIn("build", out)
        with mock.patch.object(sshtsf, "live_sessions", return_value=([], "")) as live:
            rc, out, _ = run_capture(["-L", "build"])
        self.assertEqual(rc, 0)
        self.assertEqual(live.call_args.args, ("build.internal",))

    def test_live_rows_are_worded_and_matched_by_name(self):
        rows = ["web\t1\tattached", "scratch\t3\tdetached"]
        with mock.patch.object(sshtsf, "live_sessions", return_value=(rows, "")):
            rc, out, _ = run_capture(["-L", "devbox"])
        self.assertEqual(rc, 0)
        self.assertIn("    web  1 window, attached\n", out)
        self.assertIn("    scratch  3 windows, detached   [unregistered]\n", out)
        rc, _, err = run_capture(["-L", "nowhere"])
        self.assertEqual(rc, 1)
        self.assertIn("unknown host: nowhere", err)

    def test_new_session_flag_proposes_the_second_word(self):
        with mock.patch.object(sshtsf, "route_add_session", return_value=0) as add:
            rc, _, _ = run_capture(["-n", "c", "api2", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(add.call_args.args[:3], (mock.ANY, "devbox", "api2"))
        rc, _, err = run_capture(["-n", "nowhere"])
        self.assertEqual(rc, 1)
        self.assertIn("`sshtsf -c nowhere` registers it", err)


class TestRemoteSh(unittest.TestCase):
    def test_path_goes_first(self):
        self.assertEqual(sshtsf.remote_sh("tmux -V"),
                         ["sh", "-c", REMOTE_PATH_LINE + "tmux -V"])

    def test_env_knob_replaces_the_list(self):
        with mock.patch.object(sshtsf, "REMOTE_PATH", "/opt/tmux/bin"):
            self.assertEqual(sshtsf.remote_sh("tmux -V")[2],
                             'PATH="/opt/tmux/bin:$PATH"; tmux -V')

    def test_live_sessions_probe_uses_it(self):
        proc = mock.Mock(returncode=0, stdout="web\t2\tdetached\n", stderr="")
        with mock.patch.object(sshtsf.subprocess, "run", return_value=proc) as run:
            sessions, why = sshtsf.live_sessions("devbox")
        self.assertEqual((sessions, why), (["web\t2\tdetached"], ""))
        argv = run.call_args.args[0]
        self.assertEqual(argv[:6], PROBE + ["devbox", "sh", "-c"])
        # Quoted once for the remote login shell; sh then sees the script.
        # -u, or a locale-less tmux turns the tabs into underscores.
        script = shlex.split(argv[6])[0]
        self.assertTrue(script.startswith(REMOTE_PATH_LINE + "tmux -u list-sessions -F "))

    def test_remote_tmux_probe_uses_it(self):
        proc = mock.Mock(returncode=0, stdout="/opt/homebrew/bin/tmux\ntmux 3.5a\n")
        with mock.patch.object(sshtsf.subprocess, "run", return_value=proc) as run:
            self.assertEqual(sshtsf.remote_tmux("mac"), "/opt/homebrew/bin/tmux (tmux 3.5a)")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:8], PROBE + ["-o", "BatchMode=yes", "mac", "sh", "-c"])
        self.assertIn("command -v tmux && tmux -V", argv[8])


class TestProbes(ConfigDirMixin, unittest.TestCase):
    """What sshtsf asks a remote before or beside a connection."""

    def setUp(self):
        super().setUp()
        sshtsf.save_config(SAMPLE)

    def test_every_probe_is_bounded(self):
        # A host that is down used to hold -L, the tmux diagnosis and the
        # socket cleanup for the whole TCP connect timeout.
        ok = mock.Mock(returncode=0, stdout="/tmp/emacs-remote-socket-me\n", stderr="")
        with mock.patch.object(sshtsf.subprocess, "run", return_value=ok) as run, \
             mock.patch.object(sshtsf, "ecf_local_socket", return_value="/x/server"), \
             mock.patch.object(sshtsf.os, "execvp"):
            sshtsf.live_sessions("devbox")
            sshtsf.remote_tmux("devbox")
            sshtsf.remote_dirs("devbox")
            sshtsf.waypipe_remote_missing("devbox")
            run_capture(["devbox", "api"])  # the cleanup ahead of the connection
        argvs = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(argvs), 5)
        for argv in argvs:
            self.assertEqual(argv[:3], PROBE, argv)

    def test_a_missing_ssh_is_an_answer_not_a_crash(self):
        # These used to raise FileNotFoundError out of sshtsf.
        gone = FileNotFoundError(2, "No such file or directory", "ssh")
        with mock.patch.object(sshtsf.subprocess, "run", side_effect=gone):
            self.assertEqual(sshtsf.remote_tmux("devbox"), "")
            rows, why = sshtsf.live_sessions("devbox")
            self.assertEqual(rows, [])
            self.assertIn("cannot run ssh", why)
            self.assertEqual(sshtsf.remote_dirs("devbox"), [])
            self.assertEqual(sshtsf.waypipe_remote_missing("devbox"), "")
            rc, out, _ = run_capture(["-L", "devbox"])
            self.assertEqual(rc, 0)
            self.assertIn("devbox:\n    (cannot run ssh:", out)

    def test_cleanup_without_ssh_drops_the_forward_and_goes_on(self):
        gone = FileNotFoundError(2, "No such file or directory", "ssh")
        with mock.patch.object(sshtsf.subprocess, "run", side_effect=gone), \
             mock.patch.object(sshtsf, "ecf_local_socket", return_value="/x/server"), \
             mock.patch.object(sshtsf.os, "execvp") as execvp:
            _, _, err = run_capture(["devbox", "api"])
        self.assertIn("could not clear the Emacs socket on devbox ([Errno 2] No such file "
                      "or directory: 'ssh'); connecting without the forward", err)
        self.assertNotIn("-R", execvp.call_args.args[1])


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
        self.assertEqual(argv, PROBE + ["-o", "BatchMode=yes", "devbox", "command -v waypipe"])

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


class TestConfigure(ConfigDirMixin, unittest.TestCase):
    """-c: the same prompt walk as registering, over an entry that exists."""

    def setUp(self):
        super().setUp()
        sshtsf.save_config(SAMPLE)

    def configure(self, argv, answers, picked=None, folder="src/webapp"):
        with mock.patch.object(sshtsf, "ask", side_effect=scripted(answers)), \
             mock.patch.object(sshtsf, "choose", return_value=picked), \
             mock.patch.object(sshtsf, "prompt_folder", return_value=folder), \
             mock.patch.object(sshtsf, "connect") as connect:
            rc, out, err = run_capture(["-c"] + argv)
        connect.assert_not_called()
        return rc, err, sshtsf.load_config()

    def test_blank_keeps_and_dash_clears(self):
        rc, _, cfg = self.configure(["devbox", "web"], {
            "session name": "", "command to run": "-",
            "forward the local Emacs socket": "", "forward Wayland": "", "alias for": ""})
        self.assertEqual(rc, 0)
        self.assertEqual(cfg["hosts"]["devbox"]["sessions"]["web"],
                         {"alias": "devweb", "folder": "src/webapp"})

    def test_session_flags_are_set_and_unset(self):
        rc, _, cfg = self.configure(["devbox", "api"], {
            "session name": "", "command to run": "",
            "forward the local Emacs socket": "-", "forward Wayland": "yes", "alias for": ""},
            folder="src/api")
        self.assertEqual(rc, 0)
        self.assertEqual(cfg["hosts"]["devbox"]["sessions"]["api"],
                         {"folder": "src/api", "waypipe": True})

    def test_rename_session_moves_last(self):
        rc, _, cfg = self.configure(["devbox", "devweb"], {
            "session name": "www", "command to run": "",
            "forward the local Emacs socket": "", "forward Wayland": "", "alias for": "-"})
        self.assertEqual(rc, 0)
        sessions = cfg["hosts"]["devbox"]["sessions"]
        self.assertNotIn("web", sessions)
        self.assertEqual(sessions["www"], {"folder": "src/webapp", "command": "make dev"})
        self.assertEqual(cfg["last"], {"host": "devbox", "session": "www"})

    def test_remove_session_after_a_yes(self):
        rc, _, cfg = self.configure(["devbox", "web"], {"session name": "-", "remove": "n"})
        self.assertEqual(rc, 0)
        self.assertIn("web", cfg["hosts"]["devbox"]["sessions"])
        rc, _, cfg = self.configure(["devbox", "web"], {"session name": "-", "remove": "y"})
        self.assertEqual(rc, 0)
        self.assertNotIn("web", cfg["hosts"]["devbox"]["sessions"])
        self.assertNotIn("last", cfg)  # last pointed at the removed session

    def test_host_settings_default_and_port(self):
        rc, _, cfg = self.configure(["build"], {
            "name": "", "ssh destination": "", "alias": "b",
            "forward the local Emacs socket": "y", "relay it": "-",
            "forward Wayland": "", "offer this host first": "y"},
            picked=sshtsf.HOST_SETTINGS)
        self.assertEqual(rc, 0)
        self.assertEqual(cfg["hosts"]["build"],
                         {"target": "build.internal", "alias": "b", "ecf": True})
        self.assertEqual(cfg["default_host"], "build")

    def test_rename_host_follows_default_and_last(self):
        rc, _, cfg = self.configure(["devbox"], {
            "name": "dev", "ssh destination": "", "alias": "",
            "forward the local Emacs socket": "", "forward Wayland": "",
            "offer this host first": ""},
            picked=sshtsf.HOST_SETTINGS)
        self.assertEqual(rc, 0)
        self.assertNotIn("devbox", cfg["hosts"])
        # The destination was the old name, so it is now stated.
        self.assertEqual(cfg["hosts"]["dev"]["target"], "devbox")
        self.assertEqual(cfg["hosts"]["dev"]["alias"], "c")
        self.assertIs(cfg["hosts"]["dev"]["ecf"], False)
        self.assertEqual(cfg["default_host"], "dev")
        self.assertEqual(cfg["last"]["host"], "dev")

    def test_remove_host_with_sessions_asks(self):
        rc, _, cfg = self.configure(["c"], {"name": "-", "remove devbox and its 3": "n"},
                                    picked=sshtsf.HOST_SETTINGS)
        self.assertEqual(rc, 0)
        self.assertIn("devbox", cfg["hosts"])
        rc, _, cfg = self.configure(["c"], {"name": "-", "remove devbox and its 3": "y"},
                                    picked=sshtsf.HOST_SETTINGS)
        self.assertEqual(rc, 0)
        self.assertNotIn("devbox", cfg["hosts"])
        self.assertNotIn("default_host", cfg)
        self.assertNotIn("last", cfg)

    def test_unknown_names_register_without_connecting(self):
        rc, err, cfg = self.configure(["newbox"], {
            "name for this [user@]host": "", "ssh destination": "", "user on it": "",
            "alias": "", "forward the local Emacs socket": "", "forward Wayland": ""})
        self.assertEqual(rc, 0)
        self.assertIn("new host newbox", err)
        self.assertEqual(cfg["hosts"]["newbox"], {})
        rc, err, cfg = self.configure(["devbox", "api2"], {
            "session name": "", "command to run": "", "forward the local Emacs socket": "",
            "forward Wayland": "", "alias for": ""}, folder="")
        self.assertEqual(rc, 0)
        self.assertIn("new session on devbox", err)
        self.assertEqual(cfg["hosts"]["devbox"]["sessions"]["api2"], {"alias": "api2"})

    def test_a_new_session_is_offered_its_name_as_alias_only_while_free(self):
        # devweb is devbox+web's alias, so a session of that name on build
        # gets no alias proposed: a blank answer leaves it without one.
        rc, _, cfg = self.configure(["build", "devweb"], {
            "session name": "", "command to run": "", "forward the local Emacs socket": "",
            "forward Wayland": "", "alias for build+devweb (blank for none)": ""}, folder="")
        self.assertEqual(rc, 0)
        self.assertEqual(cfg["hosts"]["build"]["sessions"]["devweb"], {})

    def test_a_taken_alias_is_asked_again(self):
        rc, err, cfg = self.configure(["build", "api"], {
            "session name": "", "command to run": "", "forward the local Emacs socket": "",
            "forward Wayland": "", "alias for": ["c", "devbox", "bapi"]}, folder="")
        self.assertEqual(rc, 0)
        self.assertIn("  c already names host devbox\n", err)
        self.assertIn("  devbox already names host devbox\n", err)
        self.assertEqual(cfg["hosts"]["build"]["sessions"]["api"], {"alias": "bapi"})
        rc, err, cfg = self.configure(["build"], {
            "name": "", "ssh destination": "", "alias": ["devweb", "b"],
            "forward the local Emacs socket": "", "relay it": "", "forward Wayland": "",
            "offer this host first": ""}, picked=sshtsf.HOST_SETTINGS)
        self.assertEqual(rc, 0)
        self.assertIn("  devweb already names devbox+web\n", err)
        self.assertEqual(cfg["hosts"]["build"]["alias"], "b")

    def test_a_host_keeps_its_own_words_but_takes_no_others(self):
        # Renaming devbox to its own alias is fine; to another host's name
        # is asked again, where it used to end the walk.
        rc, err, cfg = self.configure(["devbox"], {
            "name": ["build", "c"], "ssh destination": "", "alias": "-",
            "forward the local Emacs socket": "", "forward Wayland": "",
            "offer this host first": ""}, picked=sshtsf.HOST_SETTINGS)
        self.assertEqual(rc, 0)
        self.assertIn("  build already names host build\n", err)
        self.assertEqual(sorted(cfg["hosts"]), ["build", "c"])
        self.assertNotIn("alias", cfg["hosts"]["c"])

    def test_a_new_host_named_like_a_session_alias_is_asked_again(self):
        with mock.patch.object(sshtsf, "ask", side_effect=scripted({
                "name for this [user@]host": ["devweb", "dw"], "ssh destination": "",
                "user on it": "", "alias": "", "forward the local Emacs socket": "",
                "forward Wayland": ""})), \
             mock.patch.object(sshtsf, "ssh_host_candidates", return_value=[]), \
             mock.patch.object(sshtsf, "route_add_session", return_value=0), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            sshtsf.route_add_host(sshtsf.load_config(), "devweb.local")
        self.assertIn("  devweb already names devbox+web\n", err.getvalue())
        self.assertEqual(sshtsf.load_config()["hosts"]["dw"], {"target": "devweb.local"})

    def test_leaving_the_folder_picker_does_not_end_the_walk(self):
        answers = {"session name": "", "folder": "?", "command to run": "",
                   "forward the local Emacs socket": "", "forward Wayland": "",
                   "alias for": ""}
        with mock.patch.object(sshtsf, "ask", side_effect=scripted(answers)), \
             mock.patch.object(sshtsf, "remote_dirs", return_value=["src/x"]), \
             mock.patch.object(sshtsf, "choose", return_value=None):
            rc, _, err = run_capture(["-c", "devbox", "web"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(sshtsf.load_config()["hosts"]["devbox"]["sessions"]["web"],
                         SAMPLE["hosts"]["devbox"]["sessions"]["web"])

    def test_picker_lists_settings_sessions_and_new(self):
        with mock.patch.object(sshtsf, "choose", return_value=None) as pick:
            rc, _, _ = run_capture(["-c", "devbox"])
        self.assertEqual(rc, 130)
        items = pick.call_args.args[0]
        self.assertEqual(items[0], sshtsf.HOST_SETTINGS)
        self.assertEqual(items[-1], sshtsf.NEW_SESSION)
        self.assertEqual(len(items), 5)
        with mock.patch.object(sshtsf, "choose", return_value=None) as pick:
            rc, _, _ = run_capture(["-c"])
        self.assertEqual(rc, 130)
        self.assertEqual(pick.call_args.args[0][-1], sshtsf.NEW_HOST)


if __name__ == "__main__":
    unittest.main()
