"""distbuild makepkg: the system flags with the CPU named, merged into the
user's makepkg.conf."""

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
        self.assertEqual([(o.name, str(o.source)) for o in got][:1],
                         [("CFLAGS", "/etc/makepkg.conf.d/zz.conf")])

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

    def test_no_native_march(self):
        with mock.patch.object(distbuild, "native_march", return_value=None):
            rc, _, err = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("name the TARGET", err)


if __name__ == "__main__":
    unittest.main()
