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

    def test_localslots(self):
        self.run_cli("--localslots", "4")
        self.assertIn('DISTCC_HOSTS="10.0.0.5/20 --localslots=4"', self.user.read_text())
        self.assertEqual(self.run_cli("--localslots", "0")[0], 2)

    def test_attr_replaces_the_default_term(self):
        self.run_cli("-a", "mac", "-n")
        self.assertEqual(self.probed, ["alice@my-mac.local"])


if __name__ == "__main__":
    unittest.main()
