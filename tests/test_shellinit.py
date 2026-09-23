"""curiousj-tools init: what it prints, and the shell file behaving alike
under bash and zsh."""

from __future__ import annotations

import contextlib
import io
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from curiousj_tools import shellinit

SCRIPT = pathlib.Path(str(shellinit.script_file()))
SHELLS = [s for s in ("bash", "zsh") if shutil.which(s)]


def run_cli(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        rc = shellinit.main(list(argv))
    return rc, out.getvalue()


class TestInit(unittest.TestCase):
    def test_prints_the_file_for_either_shell(self):
        for shell in ("bash", "zsh"):
            self.assertEqual(run_cli("init", shell), (0, SCRIPT.read_text()))

    def test_path(self):
        rc, out = run_cli("init", "--path", "zsh")
        self.assertEqual(rc, 0)
        self.assertEqual(pathlib.Path(out.strip()), SCRIPT)
        self.assertTrue(SCRIPT.is_file())

    def test_unknown_shell_is_usage(self):
        self.assertEqual(run_cli("init", "fish")[0], 2)


class ShellCase(unittest.TestCase):
    """Runs a snippet after sourcing the file, in each shell present, with a
    bin directory of fakes ahead of the system PATH."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bin = pathlib.Path(tmp.name)

    def fake(self, name: str, body: str = "exit 0") -> pathlib.Path:
        p = self.bin / name
        p.write_text(f"#!/bin/sh\n{body}\n")
        p.chmod(0o755)
        return p

    def run_in(self, shell: str, snippet: str, **env: str) -> str:
        # Nothing inherited: a test run inside tmux or a Wayland session
        # would otherwise see its own values.
        base = dict(PATH=f"{self.bin}:/usr/bin:/bin", HOME=str(self.bin), USER="alice", **env)
        proc = subprocess.run([shell, "-c", f'. "{SCRIPT}"\n{snippet}'],
                              env=base, capture_output=True, text=True)
        self.assertEqual(proc.stderr, "")
        return proc.stdout


class TestSyntax(unittest.TestCase):
    def test_parses(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                subprocess.run([shell, "-n", str(SCRIPT)], check=True)


class TestEmacsRemote(ShellCase):
    def test_routes_and_restores(self):
        auto = self.fake("emacsclient-auto")
        browse = self.fake("ec-browse")
        for shell in SHELLS:
            with self.subTest(shell=shell):
                out = self.run_in(shell, """
                    emacs-remote ssh alice@devbox
                    echo "$EMACSCLIENT_TRAMP_PREFIX|$EDITOR|$BROWSER"
                    emacs-remote off
                    echo "${EMACSCLIENT_TRAMP_PREFIX-unset}|$EDITOR|${BROWSER-unset}"
                """)
                self.assertEqual(out.splitlines(), [
                    f"/ssh:alice@devbox:|{auto}|{browse}",
                    "unset|emacsclient|unset",
                ])

    def test_target_without_user_gets_this_login(self):
        self.fake("emacsclient-auto")
        for shell in SHELLS:
            with self.subTest(shell=shell):
                out = self.run_in(shell, 'emacs-remote sshx; echo "$EMACSCLIENT_TRAMP_PREFIX"',
                                  EMACS_REMOTE_TARGET="devbox.local")
                self.assertEqual(out.strip(), "/sshx:alice@devbox.local:")

    def test_without_emacsclient_auto(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                base = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.bin)}
                proc = subprocess.run(
                    [shell, "-c", f'. "{SCRIPT}"; emacs-remote; echo "rc=$? ${{EDITOR-unset}}"'],
                    env=base, capture_output=True, text=True)
                self.assertEqual(proc.stdout.strip(), "rc=1 unset")
                self.assertIn("emacsclient-auto not on PATH", proc.stderr)


class TestTmuxEnvRefresh(ShellCase):
    SHOW_ENVIRONMENT = "\n".join([
        'WAYLAND_DISPLAY="wayland-9"; export WAYLAND_DISPLAY;',
        "unset DISPLAY;",
        'FOO="nope"; export FOO;',
        'CMUX_TAB_ID="t1"; export CMUX_TAB_ID;',
        'EMACS_REMOTE_TARGET="bob@devbox"; export EMACS_REMOTE_TARGET;',
    ])

    def setUp(self):
        super().setUp()
        self.fake("emacsclient-auto")
        self.fake("tmux", f"cat <<'EOF'\n{self.SHOW_ENVIRONMENT}\nEOF")

    def test_refreshes_listed_and_extra_names_only(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                out = self.run_in(shell, """
                    emacs-remote sshx alice@old
                    tmux-env-refresh
                    echo "$WAYLAND_DISPLAY|$DISPLAY|${FOO-unset}|$CMUX_TAB_ID"
                    echo "$EMACSCLIENT_TRAMP_PREFIX"
                """, TMUX="/tmp/tmux-1/default,1,0", DISPLAY=":0",
                    EMACS_REMOTE_TARGET="alice@old", TMUX_ENV_REFRESH_EXTRA="CMUX_PANEL_ID CMUX_TAB_ID")
                self.assertEqual(out.splitlines(), [
                    "wayland-9|:0|unset|t1",
                    # the new target, with the method chosen before
                    "/sshx:bob@devbox:",
                ])

    def test_unrouted_shell_stays_unrouted(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                out = self.run_in(shell, 'tmux-env-refresh; echo "$EMACS_REMOTE_TARGET|${EMACSCLIENT_TRAMP_PREFIX-unset}"',
                                  TMUX="/tmp/tmux-1/default,1,0")
                self.assertEqual(out.strip(), "bob@devbox|unset")

    def test_outside_tmux_does_nothing(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                out = self.run_in(shell, 'tmux-env-refresh; echo "${WAYLAND_DISPLAY-unset}"')
                self.assertEqual(out.strip(), "unset")

    def test_hook_registered_once(self):
        cases = {"bash": 'echo "$PROMPT_COMMAND"', "zsh": 'echo "$precmd_functions"'}
        for shell in SHELLS:
            with self.subTest(shell=shell):
                out = self.run_in(shell, f'. "{SCRIPT}"\n{cases[shell]}')
                self.assertEqual(out.strip(), "tmux-env-refresh")


if __name__ == "__main__":
    unittest.main()
