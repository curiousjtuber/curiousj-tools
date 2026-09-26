"""distbuild makepkg: the system flags with the CPU named, merged into the
user's makepkg.conf; distbuild hosts: the distcc settings for the logins."""

from __future__ import annotations

import contextlib
import io
import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from unittest import mock

import click

from curiousj_tools import distbuild
from curiousj_tools.lists import Login

SYSTEM = textwrap.dedent("""\
    #!/hint/bash
    #-- Compiler and Linker Flags
    #CPPFLAGS=""
    CFLAGS="-march=native -O3 -pipe -fno-plt \\
            -fstack-clash-protection -fcf-protection"
    CXXFLAGS="$CFLAGS -Wp,-D_GLIBCXX_ASSERTIONS"
    LDFLAGS="-Wl,-O1"
    DEBUG_CFLAGS="-g"
    DEBUG_CXXFLAGS="$DEBUG_CFLAGS"
    DLAGENTS=('file::/usr/bin/curl -qgC - -o %o %u'
              'https::/usr/bin/curl -qgb "" -fLC - --retry 3 --retry-delay 3 -o %o %u')
    BUILDENV=(!distcc color !ccache check !sign)  # don't
    """)

RUST = textwrap.dedent("""\
    RUSTFLAGS="-C opt-level=3 -C target-cpu=native"
    DEBUG_RUSTFLAGS="-C debuginfo=2"
    """)


def confs(system: str = SYSTEM, rust: str | None = RUST) -> list[tuple[pathlib.Path, str]]:
    out = [(pathlib.Path("/etc/makepkg.conf"), system)]
    if rust is not None:
        out.append((pathlib.Path("/etc/makepkg.conf.d/rust.conf"), rust))
    return out


def merged(user: str, system: str = SYSTEM, rust: str | None = RUST) -> str:
    return distbuild.apply(user, distbuild.overrides(confs(system, rust), "znver4", "znver4"))


class TestAssignments(unittest.TestCase):
    def test_multiline_values_are_one_assignment(self):
        names = [a.name for a in distbuild.assignments(SYSTEM)]
        self.assertEqual(names, ["CFLAGS", "CXXFLAGS", "LDFLAGS", "DEBUG_CFLAGS",
                                 "DEBUG_CXXFLAGS", "DLAGENTS", "BUILDENV"])

    def test_continued_line_is_kept_whole(self):
        cflags = distbuild.assignments(SYSTEM)[0].text
        self.assertTrue(cflags.startswith('CFLAGS="-march=native'))
        self.assertTrue(cflags.endswith('-fcf-protection"'))

    def test_line_inside_a_value_is_not_an_assignment(self):
        text = 'A="x\nB=y"\nC=z\n'
        self.assertEqual([a.name for a in distbuild.assignments(text)], ["A", "C"])


class TestOverrides(unittest.TestCase):
    def test_names_the_cpu_and_takes_what_builds_on_it(self):
        got = [(o.name, o.text) for o in distbuild.overrides(confs(), "znver4", "x86-64-v3")]
        self.assertEqual(got, [
            ("CFLAGS", 'CFLAGS="-march=znver4 -O3 -pipe -fno-plt \\\n'
                       '        -fstack-clash-protection -fcf-protection"'),
            ("CXXFLAGS", 'CXXFLAGS="$CFLAGS -Wp,-D_GLIBCXX_ASSERTIONS"'),
            ("RUSTFLAGS", 'RUSTFLAGS="-C opt-level=3 -C target-cpu=x86-64-v3"'),
        ])

    def test_any_march_and_a_native_mtune(self):
        system = 'CFLAGS="-march=x86-64 -mtune=native -O2"\n'
        [o] = distbuild.overrides(confs(system, None), "znver4", "znver4")
        self.assertEqual(o.text, 'CFLAGS="-march=znver4 -mtune=znver4 -O2"')

    def test_a_later_file_wins(self):
        later = (pathlib.Path("/etc/makepkg.conf.d/zz.conf"), 'CFLAGS="-march=native -O2"\n')
        got = distbuild.overrides(confs() + [later], "znver4", "znver4")
        self.assertEqual([(o.name, o.note) for o in got][:1],
                         [("CFLAGS", "from /etc/makepkg.conf.d/zz.conf")])

    def test_without_rust_conf(self):
        got = distbuild.overrides(confs(rust=None), "znver4", "znver4")
        self.assertEqual([o.name for o in got], ["CFLAGS", "CXXFLAGS"])


class TestApply(unittest.TestCase):
    def test_new_file(self):
        self.assertEqual(merged(""), textwrap.dedent("""\
            #-- from /etc/makepkg.conf
            CFLAGS="-march=znver4 -O3 -pipe -fno-plt \\
                    -fstack-clash-protection -fcf-protection"
            CXXFLAGS="$CFLAGS -Wp,-D_GLIBCXX_ASSERTIONS"

            #-- from /etc/makepkg.conf.d/rust.conf
            RUSTFLAGS="-C opt-level=3 -C target-cpu=znver4"
            """))

    def test_replaces_in_place_and_keeps_the_rest(self):
        user = textwrap.dedent("""\
            # mine
            CFLAGS="-march=skylake -O2 \\
                    -pipe"
            MAKEFLAGS="-j70"
            RUSTFLAGS="-C target-cpu=skylake"
            BUILDENV=(distcc color !ccache check !sign)
            """)
        self.assertEqual(merged(user), textwrap.dedent("""\
            # mine
            CFLAGS="-march=znver4 -O3 -pipe -fno-plt \\
                    -fstack-clash-protection -fcf-protection"
            CXXFLAGS="$CFLAGS -Wp,-D_GLIBCXX_ASSERTIONS"
            MAKEFLAGS="-j70"
            RUSTFLAGS="-C opt-level=3 -C target-cpu=znver4"
            BUILDENV=(distcc color !ccache check !sign)
            """))

    def test_last_setting_is_the_one_replaced(self):
        user = 'CFLAGS="-O1"\nCFLAGS="-O2"\nCXXFLAGS="-O2"\n'
        out = merged(user, rust=None)
        self.assertTrue(out.startswith('CFLAGS="-O1"\nCFLAGS="-march=znver4'))
        self.assertTrue(out.endswith('CXXFLAGS="$CFLAGS -Wp,-D_GLIBCXX_ASSERTIONS"\n'))

    def test_appends_after_one_blank_line(self):
        out = merged('MAKEFLAGS="-j70"\n\n', rust=None)
        self.assertTrue(out.startswith('MAKEFLAGS="-j70"\n\n#-- from /etc/makepkg.conf\nCFLAGS='))

    def test_idempotent(self):
        once = merged('MAKEFLAGS="-j70"')
        self.assertEqual(merged(once), once)

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_bash_sees_the_named_cpu(self):
        with tempfile.TemporaryDirectory() as d:
            system, user = pathlib.Path(d, "system"), pathlib.Path(d, "user")
            system.write_text(SYSTEM + RUST)
            user.write_text(merged('BUILDENV=(distcc)\n'))
            out = subprocess.run(
                ["bash", "-c", f'. "{system}"; . "{user}"; echo "$CXXFLAGS"; echo "$RUSTFLAGS"'],
                capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.splitlines(), [
            "-march=znver4 -O3 -pipe -fno-plt         -fstack-clash-protection -fcf-protection"
            " -Wp,-D_GLIBCXX_ASSERTIONS",
            "-C opt-level=3 -C target-cpu=znver4",
        ])


class TestSetOption(unittest.TestCase):
    def test_set_option(self):
        cases = {("OPTIONS=(strip !debug lto !autodeps)", "lto", False): "OPTIONS=(strip !debug !lto !autodeps)",
                 ("OPTIONS=(strip !lto)", "lto", True): "OPTIONS=(strip lto)",
                 ("OPTIONS=(!lto-extra lto)", "lto", False): "OPTIONS=(!lto-extra !lto)",
                 ("OPTIONS=(strip)", "lto", False): "OPTIONS=(!lto strip)",
                 ("BUILDENV=(!distcc color !ccache)", "distcc", True): "BUILDENV=(distcc color !ccache)",
                 ("BUILDENV=(distcc color)", "distcc", True): "BUILDENV=(distcc color)",
                 ("BUILDENV=(color check)", "distcc", True): "BUILDENV=(distcc color check)",
                 ("BUILDENV=()", "distcc", True): "BUILDENV=(distcc )",
                 ("BUILDENV=(!distcc-x distcc)", "distcc", False): "BUILDENV=(!distcc-x !distcc)"}
        for (before, word, on), after in cases.items():
            self.assertEqual(distbuild.set_option(before, word, on), after)


class TestCli(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        etc = self.root / "etc"
        (etc / "makepkg.conf.d").mkdir(parents=True)
        (etc / "makepkg.conf").write_text(SYSTEM)
        (etc / "makepkg.conf.d" / "rust.conf").write_text(RUST)
        self.user = self.root / "config" / "pacman" / "makepkg.conf"
        for patch in (mock.patch.object(distbuild, "SYSTEM_CONF", etc / "makepkg.conf"),
                      mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config"),
                                                   "HOME": str(self.root)}),
                      mock.patch.object(distbuild, "native_march", return_value="znver4"),
                      mock.patch.object(distbuild, "native_rust_cpu", return_value="znver5")):
            patch.start()
            self.addCleanup(patch.stop)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = distbuild.main(["makepkg", *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_creates_the_file(self):
        rc, out, _ = self.run_cli()
        self.assertEqual(rc, 0)
        self.assertIn("-march=znver4", self.user.read_text())
        self.assertIn("target-cpu=znver5", self.user.read_text())
        self.assertIn("CFLAGS, CXXFLAGS, RUSTFLAGS", out)

    def test_target_is_the_rust_cpu_too_unless_given(self):
        self.run_cli("x86-64-v3")
        self.assertIn("target-cpu=x86-64-v3", self.user.read_text())
        self.run_cli("--rust-cpu", "znver4", "x86-64-v3")
        self.assertIn("target-cpu=znver4", self.user.read_text())

    def test_dry_run_writes_nothing(self):
        rc, out, _ = self.run_cli("-n")
        self.assertEqual(rc, 0)
        self.assertFalse(self.user.exists())
        self.assertIn("+CFLAGS=\"-march=znver4", out)

    def test_unchanged_says_so(self):
        self.run_cli()
        self.assertIn("(unchanged)", self.run_cli()[1])

    def test_legacy_file_is_refused(self):
        (self.root / ".makepkg.conf").write_text("")
        rc, _, err = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("move it to", err)
        self.assertFalse(self.user.exists())

    def test_no_lto_from_the_system_options(self):
        (self.root / "etc" / "makepkg.conf").write_text(SYSTEM + "OPTIONS=(strip lto !debug)\n")
        self.run_cli("--no-lto")
        self.assertIn("OPTIONS=(strip !lto !debug)", self.user.read_text())
        self.run_cli("--lto")
        self.assertIn("OPTIONS=(strip lto !debug)", self.user.read_text())

    def test_options_left_alone_by_default(self):
        (self.root / "etc" / "makepkg.conf").write_text(SYSTEM + "OPTIONS=(strip lto !debug)\n")
        self.run_cli()
        self.assertNotIn("OPTIONS", self.user.read_text())

    def test_no_native_march(self):
        with mock.patch.object(distbuild, "native_march", return_value=None):
            rc, _, err = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("name the TARGET", err)


class TestProbe(unittest.TestCase):
    OUT = ("16\n--distbuild--\n"
           'DISTCC_ARGS="--allow-private --listen=10.0.0.5"\n--distbuild--\n'
           "wired 10.0.0.5\nwifi 10.0.1.5\n")

    def test_parse(self):
        self.assertEqual(distbuild.parse_probe(self.OUT),
                         distbuild.Probed(16, "10.0.0.5", ["10.0.0.5"], ["10.0.1.5"]))

    def test_parse_rejects_what_is_not_the_script(self):
        self.assertIsNone(distbuild.parse_probe("Welcome!\n"))

    def test_listen_address_forms(self):
        for args in ("--listen=10.0.0.5", "--listen 10.0.0.5", "--allow-private --listen  10.0.0.5"):
            self.assertEqual(distbuild.listen_address(f'DISTCC_ARGS="{args}"\n'), "10.0.0.5")
        self.assertIsNone(distbuild.listen_address('DISTCC_ARGS="--allow-private"\n'))
        self.assertIsNone(distbuild.listen_address('#DISTCC_ARGS="--listen=10.0.0.5"\n'))

    def test_wired_before_wifi_before_the_name(self):
        found = distbuild.Probed(16, None, ["10.0.0.5"], ["10.0.1.5"])
        self.assertEqual(distbuild.candidates(found, "devbox.local"),
                         (["10.0.0.5", "10.0.1.5", "devbox.local"], None))

    def test_listen_is_the_only_address(self):
        found = distbuild.Probed(16, "10.0.0.5", ["10.0.0.5"], ["10.0.1.5"])
        self.assertEqual(distbuild.candidates(found, "devbox"), (["10.0.0.5"], None))

    def test_listening_on_wifi_beside_a_wire_is_reported(self):
        found = distbuild.Probed(16, "10.0.1.5", ["10.0.0.5"], ["10.0.1.5"])
        tried, why = distbuild.candidates(found, "devbox")
        self.assertEqual(tried, ["10.0.1.5"])
        self.assertIn("--listen 10.0.0.5", why)

    def test_first_answering_address(self):
        run = subprocess.CompletedProcess([], 0, stdout=self.OUT.replace("--listen=10.0.0.5", ""), stderr="")
        with mock.patch.object(distbuild.subprocess, "run", return_value=run), \
             mock.patch.object(distbuild.sshutil, "ssh_config", return_value={}), \
             mock.patch.object(distbuild, "distccd_answers", side_effect=lambda h: h == "10.0.1.5"):
            self.assertEqual(distbuild.probe(Login("alice@devbox")),
                             (distbuild.Helper("10.0.1.5", 16), []))

    def test_unreachable(self):
        run = subprocess.CompletedProcess([], 255, stdout="", stderr="Permission denied (publickey).\n")
        with mock.patch.object(distbuild.subprocess, "run", return_value=run):
            helper, warnings = distbuild.probe(Login("alice@devbox"))
        self.assertIsNone(helper)
        self.assertIn("Permission denied", warnings[0])

    @unittest.skipUnless(shutil.which("sh"), "needs sh")
    def test_script_runs_under_sh(self):
        out = subprocess.run(["sh", "-c", distbuild.PROBE_SCRIPT],
                             capture_output=True, text=True).stdout
        self.assertIsNotNone(distbuild.parse_probe(out))


class TestHostSettings(unittest.TestCase):
    def test_most_cores_first_localhost_after_its_equals(self):
        helpers = [distbuild.Helper("localhost", 16, local=True), distbuild.Helper("10.0.0.8", 8),
                   distbuild.Helper("10.0.0.5", 16), distbuild.Helper("10.0.0.5", 16)]
        self.assertEqual(distbuild.distcc_hosts(helpers, 4, 8),
                         "10.0.0.5/20 localhost/20 10.0.0.8/12 --localslots=8")

    def test_merged_into_the_user_file(self):
        user = 'CFLAGS="-march=znver4"\nMAKEFLAGS="-j70"\nDISTCC_HOSTS="old/1"\n'
        wanted = distbuild.host_overrides(user, confs(), "10.0.0.5/20 localhost/20", 56)
        self.assertEqual(distbuild.apply(user, wanted), textwrap.dedent("""\
            CFLAGS="-march=znver4"
            MAKEFLAGS="-j56"
            NINJAFLAGS="-j56"
            BUILDENV=(distcc color !ccache check !sign)  # don't
            DISTCC_HOSTS="10.0.0.5/20 localhost/20"
            """))

    def test_the_user_buildenv_wins_over_the_system_one(self):
        wanted = distbuild.host_overrides("BUILDENV=(!distcc ccache)\n", confs(), "localhost/4", 8)
        self.assertEqual([o.text for o in wanted if o.name == "BUILDENV"], ["BUILDENV=(distcc ccache)"])

    def test_native_left(self):
        self.assertTrue(distbuild.native_left("", confs()))
        self.assertFalse(distbuild.native_left('CFLAGS="-march=znver4"\n', confs()))


class TestHostsCli(unittest.TestCase):
    LISTS = textwrap.dedent("""\
        logins:
          - login: alice@devbox
            attributes: [distccd]
          - login: alice@devbox
            attributes: [distccd]
            via: distrobox enter dev --
          - login: alice@buildbox
            attributes: [distccd]
          - login: alice@my-mac.local
            attributes: [mac]
        """)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "etc").mkdir()
        (self.root / "etc" / "makepkg.conf").write_text(SYSTEM)
        self.lists = self.root / "ssh-lists.yaml"
        self.lists.write_text(self.LISTS)
        self.user = self.root / "config" / "pacman" / "makepkg.conf"
        self.probed = []
        answers = {"alice@devbox": (distbuild.Helper("10.0.0.5", 16), []),
                   "alice@buildbox": (None, ["alice@buildbox: no distccd answering"]),
                   "alice@my-mac.local": (None, [])}

        def probe(login):
            self.probed.append(login.login)
            return answers[login.login]
        for patch in (mock.patch.object(distbuild, "SYSTEM_CONF", self.root / "etc" / "makepkg.conf"),
                      mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config"),
                                                   "HOME": str(self.root)}),
                      mock.patch.object(distbuild, "probe", side_effect=probe),
                      mock.patch.object(distbuild, "local_nproc", return_value=8)):
            patch.start()
            self.addCleanup(patch.stop)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = distbuild.main(["hosts", "-f", str(self.lists), *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_distccd_logins_and_localhost(self):
        rc, out, err = self.run_cli("--local")
        self.assertEqual(rc, 0)
        self.assertEqual(self.probed, ["alice@devbox", "alice@buildbox"])
        text = self.user.read_text()
        self.assertIn('DISTCC_HOSTS="10.0.0.5/20 localhost/12 --localslots=8"', text)
        self.assertIn('MAKEFLAGS="-j40"', text)  # 20 + 12 slots, 8 here
        self.assertIn("BUILDENV=(distcc color", text)
        self.assertIn("distbuild: alice@buildbox: no distccd answering\n", err)
        self.assertIn("run `distbuild makepkg` too", err)

    def test_localhost_left_out_unless_asked(self):
        self.run_cli("-e", "0")
        text = self.user.read_text()
        self.assertIn('DISTCC_HOSTS="10.0.0.5/16 --localslots=8"', text)
        # Linking and the like still run here without a localhost slot.
        self.assertIn('MAKEFLAGS="-j24"', text)

    def test_jobs(self):
        self.run_cli("-j", "70")
        self.assertIn('NINJAFLAGS="-j70"', self.user.read_text())

    def test_no_minus_n(self):
        self.assertEqual(self.run_cli("-N")[0], 2)

    def test_slots(self):
        self.assertEqual(distbuild.slots("10.0.0.5/20 localhost/12 --localslots=8"), 32)

    def test_distcc_hosts_file(self):
        rc, out, _ = self.run_cli()
        text = (self.root / ".distcc" / "hosts").read_text()
        self.assertTrue(text.startswith("# written by `distbuild hosts`"))
        self.assertTrue(text.endswith("\n10.0.0.5/20 --localslots=8\n"))
        self.assertIn("for distcc outside makepkg: -j28 there", out)

    def test_distcc_hosts_file_under_distcc_dir(self):
        with mock.patch.dict(os.environ, {"DISTCC_DIR": str(self.root / "dd")}):
            self.run_cli()
        self.assertTrue((self.root / "dd" / "hosts").exists())
        self.assertFalse((self.root / ".distcc").exists())

    def test_localslots(self):
        self.run_cli("--localslots", "4")
        self.assertIn('DISTCC_HOSTS="10.0.0.5/20 --localslots=4"', self.user.read_text())
        self.assertEqual(self.run_cli("--localslots", "0")[0], 2)

    def test_attr_replaces_the_default_term(self):
        self.run_cli("-a", "mac", "-n")
        self.assertEqual(self.probed, ["alice@my-mac.local"])


class TestDistccdArgs(unittest.TestCase):
    def test_listen_dropped_in_either_form(self):
        for args in ("--allow-private --listen 10.0.0.5", "--listen=10.0.0.5 --allow-private"):
            self.assertEqual(distbuild.distccd_args(f'DISTCC_ARGS="{args}"\n'), "--allow-private")

    def test_other_args_kept(self):
        conf = '#DISTCC_ARGS="--jobs 2"\nDISTCC_ARGS="--allow 10.0.0.0/8 --jobs 20 --listen=10.0.0.5"\n'
        self.assertEqual(distbuild.distccd_args(conf), "--allow 10.0.0.0/8 --jobs 20")

    def test_none_admits_private_networks(self):
        for conf in ("", 'DISTCC_ARGS="--listen 10.0.0.5"\n'):
            self.assertEqual(distbuild.distccd_args(conf), "--allow-private")


class TestDistccdCli(unittest.TestCase):
    RULE = "### tuple ### allow tcp 3632 0.0.0.0/0 any 0.0.0.0/0 in\n-A ufw-user-input -p tcp --dport 3632 -j ACCEPT\n"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "distccd").write_text('DISTCC_ARGS="--allow-private --listen=10.0.0.5"\n')
        (self.root / "user.rules").write_text("")
        self.config = self.root / "config"
        # What the checks print: a machine with the system unit on, ufw on
        # without the rule, and no linger.
        self.state = {
            ("pacman", "-Q", "distcc"): "distcc 3.4-15",
            ("systemctl", "is-enabled", "distccd.service"): "enabled",
            ("systemctl", "is-active", "distccd.service"): "active",
            ("systemctl", "is-active", "ufw.service"): "active",
            ("loginctl", "show-user", "alice", "-p", "Linger", "--value"): "no",
            ("systemctl", "--user", "is-enabled", "distccd.service"): "disabled",
            ("systemctl", "--user", "is-active", "distccd.service"): "inactive",
        }
        self.ran = []
        self.fail = set()

        def run(argv):
            self.ran.append(argv)
            if argv[:3] == ["systemctl", "--user", "enable"] or argv[:3] == ["systemctl", "--user", "restart"]:
                self.state[("systemctl", "--user", "is-active", "distccd.service")] = "active"
                self.state[("systemctl", "--user", "is-enabled", "distccd.service")] = "enabled"
            return 1 if argv[0] in self.fail else 0
        for patch in (mock.patch.object(distbuild, "DISTCCD_CONF", str(self.root / "distccd")),
                      mock.patch.object(distbuild, "UFW_RULES", str(self.root / "user.rules")),
                      mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)}),
                      mock.patch.object(distbuild.sshutil, "login_name", return_value="alice"),
                      mock.patch.object(distbuild, "output", side_effect=lambda argv: self.state.get(tuple(argv), "")),
                      mock.patch.object(distbuild, "run", side_effect=run)):
            patch.start()
            self.addCleanup(patch.stop)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = distbuild.main(["distccd", *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_sets_up_in_order(self):
        rc, out, err = self.run_cli()
        self.assertEqual(rc, 0)
        self.assertEqual(self.ran, [
            ["sudo", "-v"],
            ["sudo", "systemctl", "disable", "--now", "distccd.service"],
            ["sudo", "ufw", "allow", "3632/tcp"],
            ["sudo", "loginctl", "enable-linger", "alice"],
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", "distccd.service"],
        ])
        self.assertEqual((self.config / "distccd.conf").read_text(), 'DISTCC_ARGS="--allow-private"\n')
        unit = (self.config / "systemd" / "user" / "distccd.service").read_text()
        self.assertIn(f"EnvironmentFile={self.config / 'distccd.conf'}\n", unit)
        self.assertIn("the system distccd holds the port: sudo systemctl disable", err)

    def test_nothing_left_to_do(self):
        self.run_cli()
        self.state.update({("systemctl", "is-enabled", "distccd.service"): "disabled",
                           ("systemctl", "is-active", "distccd.service"): "inactive",
                           ("loginctl", "show-user", "alice", "-p", "Linger", "--value"): "yes"})
        (self.root / "user.rules").write_text(self.RULE)
        self.ran.clear()
        rc, out, _ = self.run_cli()
        self.assertEqual((rc, self.ran), (0, []))
        self.assertIn("(unchanged)", out)

    def test_missing_distcc_is_installed_first(self):
        del self.state[("pacman", "-Q", "distcc")]
        self.run_cli()
        self.assertEqual(self.ran[1], ["sudo", "pacman", "-S", "--needed", "distcc"])

    def test_the_users_args_are_kept(self):
        (self.config).mkdir()
        (self.config / "distccd.conf").write_text('DISTCC_ARGS="--allow 10.0.0.0/8"\n')
        self.run_cli()
        self.assertEqual((self.config / "distccd.conf").read_text(), 'DISTCC_ARGS="--allow 10.0.0.0/8"\n')

    def test_changed_unit_restarts_a_running_one(self):
        self.run_cli()
        (self.config / "systemd" / "user" / "distccd.service").write_text("old\n")
        self.ran.clear()
        self.run_cli()
        self.assertEqual(self.ran[-2:], [["systemctl", "--user", "daemon-reload"],
                                         ["systemctl", "--user", "restart", "distccd.service"]])

    def test_dry_run_changes_nothing(self):
        rc, out, _ = self.run_cli("-n")
        self.assertEqual((rc, self.ran), (0, []))
        self.assertFalse(self.config.exists())
        self.assertIn("sudo systemctl disable --now distccd.service    # the system distccd holds the port", out)
        self.assertIn('DISTCC_ARGS="--allow-private"', out)

    def test_sudo_refused_changes_nothing(self):
        self.fail.add("sudo")
        rc, _, err = self.run_cli()
        self.assertEqual((rc, self.ran), (1, [["sudo", "-v"]]))
        self.assertIn("nothing changed", err)
        self.assertFalse(self.config.exists())

    def test_unreadable_ufw_rules_run_the_step(self):
        (self.root / "user.rules").unlink()
        self.run_cli()
        self.assertIn(["sudo", "ufw", "allow", "3632/tcp"], self.ran)


class TestExport(unittest.TestCase):
    def test_export_is_an_assignment(self):
        found = distbuild.assignments("export RUSTC_WRAPPER=/usr/bin/sccache\n  export  A=1\n")
        self.assertEqual([a.name for a in found], ["RUSTC_WRAPPER", "A"])


class TestSccacheCli(unittest.TestCase):
    SECRETS = distbuild.Secrets("client-tok", "server-key")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "etc").mkdir()
        (self.root / "etc" / "makepkg.conf").write_text(SYSTEM)
        (self.root / "user.rules").write_text("")
        self.secrets = self.root / "secrets.toml"
        self.secrets.write_text(distbuild.secrets_text(self.SECRETS))
        self.config = self.root / "config"
        self.attributes = {}
        self.state = {("pacman", "-Q", "sccache"): "sccache 0.17", ("pacman", "-Q", "podman"): "podman 6",
                      ("loginctl", "show-user", "alice", "-p", "Linger", "--value"): "yes"}
        self.ran = []
        self.image = 0

        def run(argv):
            self.ran.append(argv)
            if argv[:3] == ["systemctl", "--user", "enable"] or argv[:3] == ["systemctl", "--user", "restart"]:
                self.state[("systemctl", "--user", "is-active", argv[-1])] = "active"
                self.state[("systemctl", "--user", "is-enabled", argv[-1])] = "enabled"
            return 0
        machine = lambda files: Login("localhost", attributes=dict(self.attributes))
        for patch in (mock.patch.object(distbuild, "SYSTEM_CONF", self.root / "etc" / "makepkg.conf"),
                      mock.patch.object(distbuild, "UFW_RULES", str(self.root / "user.rules")),
                      mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config),
                                                   "HOME": str(self.root)}),
                      mock.patch.object(distbuild.sshutil, "login_name", return_value="alice"),
                      mock.patch.object(distbuild, "output", side_effect=lambda argv: self.state.get(tuple(argv), "")),
                      mock.patch.object(distbuild, "run", side_effect=run),
                      mock.patch.object(distbuild, "run_quiet", side_effect=lambda argv: self.image),
                      mock.patch.object(distbuild, "this_machine", side_effect=machine),
                      mock.patch.object(distbuild, "local_addresses",
                                        return_value=distbuild.Probed(16, None, ["10.0.0.5"], ["10.0.1.5"])),
                      mock.patch.object(distbuild, "sccache_dist",
                                        side_effect=lambda *a: f"token-for-{a[-1]}")):
            patch.start()
            self.addCleanup(patch.stop)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = distbuild.main(["sccache", "--secrets", str(self.secrets),
                                 "--scheduler-url", "http://10.0.0.1:10600", *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_client_only(self):
        rc, out, _ = self.run_cli()
        self.assertEqual(rc, 0)
        client = (self.config / "sccache" / "config").read_text()
        self.assertTrue(client.startswith(distbuild.CLIENT_HEADER))
        self.assertIn('scheduler_url = "http://10.0.0.1:10600"', client)
        self.assertIn('token = "client-tok"', client)
        self.assertEqual((self.config / "sccache" / "config").stat().st_mode & 0o777, 0o600)
        retry = self.root / ".local" / "share" / "distbuild" / "sccache-retry"
        self.assertIn(f"export RUSTC_WRAPPER={retry}", (self.config / "pacman" / "makepkg.conf").read_text())
        self.assertIn("-m curiousj_tools.sccache_retry", retry.read_text())
        self.assertEqual(retry.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.ran, [["sccache", "--stop-server"]])
        self.assertIn("sccache-dist client for http://10.0.0.1:10600", out)

    def test_second_run_changes_nothing(self):
        self.run_cli()
        self.ran.clear()
        rc, out, _ = self.run_cli()
        self.assertEqual((rc, self.ran), (0, []))
        self.assertIn("(unchanged)", out)
        self.assertEqual((self.config / "pacman" / "makepkg.conf").read_text().count("RUSTC_WRAPPER"), 1)

    def test_scheduler_and_server(self):
        self.attributes = {"sccache-scheduler": None, "sccache-server": None}
        self.state[("systemctl", "is-active", "ufw.service")] = "active"
        self.image = 1
        rc, out, _ = self.run_cli()
        self.assertEqual(rc, 0)
        dist = self.config / "sccache-dist"
        self.assertIn('secret_key = "server-key"', (dist / "scheduler.conf").read_text())
        server = (dist / "server.conf").read_text()
        self.assertIn('public_addr = "10.0.0.5:10501"', server)
        self.assertIn('token = "token-for-10.0.0.5:10501"', server)
        self.assertIn('type = "docker"', server)
        self.assertEqual((dist / "server.conf").stat().st_mode & 0o777, 0o600)
        shim = self.root / ".local" / "share" / "distbuild" / "podman-docker" / "docker"
        self.assertIn("-m curiousj_tools.podman_docker", shim.read_text())
        self.assertEqual(shim.stat().st_mode & 0o777, 0o755)
        unit = (self.config / "systemd" / "user" / "sccache-dist-server.service").read_text()
        self.assertIn(f"Environment=PATH={shim.parent}:/usr/bin:/bin", unit)
        self.assertEqual(self.ran[:3], [["sudo", "-v"], ["sudo", "ufw", "allow", "10600/tcp"],
                                        ["sudo", "ufw", "allow", "10501/tcp"]])
        self.assertIn(["podman", "pull", "-q", distbuild.BASE_IMAGE], self.ran)
        self.assertEqual(self.ran[-2:], [["systemctl", "--user", "enable", "--now", "sccache-dist-scheduler.service"],
                                         ["systemctl", "--user", "enable", "--now", "sccache-dist-server.service"]])
        self.assertIn("scheduler, build server, client", out)

    def test_a_system_unit_is_turned_off(self):
        self.attributes = {"sccache-server": None}
        self.state[("systemctl", "is-enabled", "sccache-server.service")] = "enabled"
        self.run_cli()
        self.assertIn(["sudo", "systemctl", "disable", "--now", "sccache-server.service"], self.ran)

    def test_someone_elses_client_config_is_refused(self):
        (self.config / "sccache").mkdir(parents=True)
        (self.config / "sccache" / "config").write_text("[cache.disk]\nsize = 1\n")
        rc, _, err = self.run_cli()
        self.assertEqual((rc, self.ran), (1, []))
        self.assertIn("is not `distbuild sccache`'s", err)

    def test_role_replaces_the_attributes(self):
        self.attributes = {"sccache-scheduler": None}
        self.run_cli("--role", "server")
        dist = self.config / "sccache-dist"
        self.assertTrue((dist / "server.conf").exists())
        self.assertFalse((dist / "scheduler.conf").exists())

    def test_the_scheduler_is_here_by_default(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = distbuild.main(["sccache", "--secrets", str(self.secrets), "--role", "scheduler"])
        self.assertEqual(rc, 0)
        self.assertIn('scheduler_url = "http://10.0.0.5:10600"',
                      (self.config / "sccache" / "config").read_text())

    def test_no_secrets(self):
        self.secrets.unlink()
        rc, _, err = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("distbuild sccache-secrets", err)

    def test_dry_run_changes_nothing(self):
        self.attributes = {"sccache-server": None}
        rc, out, _ = self.run_cli("-n")
        self.assertEqual((rc, self.ran), (0, []))
        self.assertFalse(self.config.exists())
        self.assertIn("systemctl --user enable --now sccache-dist-server.service", out)


class TestSccacheSecrets(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = pathlib.Path(tmp.name) / "s" / "secrets.toml"
        patch = mock.patch.object(distbuild, "sccache_dist", return_value="the-key")
        patch.start()
        self.addCleanup(patch.stop)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = distbuild.main(["sccache-secrets", "--secrets", str(self.path), *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_writes_them_private(self):
        self.assertEqual(self.run_cli()[0], 0)
        s = distbuild.read_secrets(self.path)
        self.assertEqual(s.server_key, "the-key")
        self.assertGreater(len(s.client_token), 30)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_kept_unless_forced(self):
        self.run_cli()
        first = self.path.read_text()
        rc, _, err = self.run_cli()
        self.assertEqual((rc, self.path.read_text()), (1, first))
        self.run_cli("--force")
        self.assertNotEqual(self.path.read_text(), first)

    def test_an_older_file_with_a_url_still_reads(self):
        self.path.parent.mkdir()
        self.path.write_text('scheduler_url = "http://x:10600"\nclient_token = "t"\nserver_key = "k"\n')
        self.assertEqual(distbuild.read_secrets(self.path), distbuild.Secrets("t", "k"))


class TestFindScheduler(unittest.TestCase):
    def test_the_lists_scheduler(self):
        with mock.patch.object(distbuild.logins, "select",
                               return_value=[Login("alice@sched", attributes={"sccache-scheduler": None})]), \
             mock.patch.object(distbuild, "login_address", return_value="10.0.0.1"):
            self.assertEqual(distbuild.find_scheduler(False, ()), "http://10.0.0.1:10600")

    def test_one_scheduler_only(self):
        with mock.patch.object(distbuild.logins, "select", return_value=[Login("a@x"), Login("b@y")]):
            with self.assertRaises(click.ClickException) as e:
                distbuild.find_scheduler(False, ())
        self.assertIn("more than one login", str(e.exception.message))

    def test_none_says_how_to_name_it(self):
        with mock.patch.object(distbuild.logins, "select", side_effect=distbuild.ToolError("no logins")):
            with self.assertRaises(click.ClickException) as e:
                distbuild.find_scheduler(False, ())
        self.assertIn("--scheduler-url", str(e.exception.message))


if __name__ == "__main__":
    unittest.main()
