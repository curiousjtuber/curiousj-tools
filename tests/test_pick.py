import io
import unittest
from unittest import mock

from curiousj_tools import pick

ITEMS = ["alpha", "beta", "gamma", "localhost"]


def answers(*replies):
    it = iter(replies)
    return lambda prompt: next(it)


class ParseMenuReply(unittest.TestCase):
    def test_numbers_spaces_and_commas(self):
        self.assertEqual(pick.parse_menu_reply("1 3", 4), [1, 3])
        self.assertEqual(pick.parse_menu_reply("2,3", 4), [2, 3])
        self.assertEqual(pick.parse_menu_reply(" 4 ", 4), [4])

    def test_all_and_abort(self):
        self.assertEqual(pick.parse_menu_reply("a", 4), [])
        self.assertEqual(pick.parse_menu_reply("A", 4), [])
        self.assertIsNone(pick.parse_menu_reply("", 4))
        self.assertIsNone(pick.parse_menu_reply("q", 4))

    def test_out_of_range_and_junk(self):
        with self.assertRaises(ValueError):
            pick.parse_menu_reply("9", 4)
        with self.assertRaises(ValueError):
            pick.parse_menu_reply("1 x", 4)
        with self.assertRaises(ValueError):
            pick.parse_menu_reply("0", 4)


class MenuPick(unittest.TestCase):
    def test_lists_items_then_returns_choice(self):
        out = io.StringIO()
        sel = pick.menu_pick(ITEMS, "hosts", answers("1 4"), out)
        self.assertEqual(sel, ["alpha", "localhost"])
        self.assertIn("  1) alpha", out.getvalue())
        self.assertIn("  4) localhost", out.getvalue())

    def test_bad_answer_reprompts(self):
        out = io.StringIO()
        sel = pick.menu_pick(ITEMS, "repos", answers("9", "2,3"), out)
        self.assertEqual(sel, ["beta", "gamma"])
        self.assertIn("not a choice: 9", out.getvalue())

    def test_all(self):
        self.assertEqual(pick.menu_pick(ITEMS, "hosts", answers("a"), io.StringIO()), ITEMS)

    def test_abort_on_q_empty_and_eof(self):
        for reply in ("q", ""):
            with self.assertRaises(pick.Abort):
                pick.menu_pick(ITEMS, "hosts", answers(reply), io.StringIO())

        def eof(prompt):
            raise EOFError

        with self.assertRaises(pick.Abort):
            pick.menu_pick(ITEMS, "hosts", eof, io.StringIO())


class FzfPick(unittest.TestCase):
    def run_fzf(self, returncode, stdout):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=returncode, stdout=stdout)
            result = pick.fzf_pick(ITEMS, "hosts")
            argv, kwargs = run.call_args[0][0], run.call_args[1]
        return result, argv, kwargs

    def test_feeds_items_and_returns_marked(self):
        sel, argv, kwargs = self.run_fzf(0, "gamma\nalpha\n")
        self.assertEqual(sel, ["gamma", "alpha"])
        self.assertEqual(argv[0], "fzf")
        self.assertIn("--multi", argv)
        self.assertIn("hosts> ", argv)
        self.assertEqual(kwargs["input"], "alpha\nbeta\ngamma\nlocalhost\n")

    def test_abort(self):
        with self.assertRaises(pick.Abort):
            self.run_fzf(130, "")
        with self.assertRaises(pick.Abort):
            self.run_fzf(0, "")


class Choose(unittest.TestCase):
    """The single-choice picker sshtsf's menus use."""

    def run_fzf(self, returncode, stdout, **kw):
        with mock.patch.object(pick, "fzf_usable", return_value=True), \
                mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=returncode, stdout=stdout)
            result = pick.choose(ITEMS, "host>", **kw)
        return result, run.call_args[0][0]

    def test_fzf_returns_the_selection_or_none(self):
        got, argv = self.run_fzf(0, "beta\n", header="hosts", query="be")
        self.assertEqual(got, "beta")
        self.assertEqual(argv[:3], ["fzf", "--prompt", "host> "])
        self.assertEqual(argv[argv.index("--header") + 1], "hosts")
        self.assertEqual(argv[argv.index("--query") + 1], "be")
        self.assertNotIn("--multi", argv)
        self.assertEqual(self.run_fzf(1, "")[0], None)
        self.assertEqual(self.run_fzf(130, "")[0], None)

    def test_fzf_free_text_falls_back_to_the_query(self):
        # --print-query: the query on line 1, a match (if any) after it.
        got, argv = self.run_fzf(1, "src/new\n", free_text=True)
        self.assertEqual(got, "src/new")
        self.assertIn("--print-query", argv)
        self.assertEqual(self.run_fzf(0, "src/\nsrc/app\n", free_text=True)[0], "src/app")
        self.assertIsNone(self.run_fzf(130, "src/\n", free_text=True)[0])

    def menu(self, replies, items=ITEMS, **kw):
        with mock.patch.object(pick, "fzf_usable", return_value=False), \
                mock.patch("builtins.input", side_effect=list(replies)), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            got = pick.choose(items, "host>", **kw)
        return got, err.getvalue()

    def test_menu_takes_a_number_a_listed_value_or_q(self):
        got, err = self.menu(["9", "x", "2"], header="hosts")
        self.assertEqual(got, "beta")
        self.assertTrue(err.startswith("hosts\n   1) alpha\n"), err)
        self.assertIn("  not a choice: 9\n  not a choice: x\n", err)
        self.assertEqual(self.menu(["gamma"])[0], "gamma")
        self.assertIsNone(self.menu(["q"])[0])
        self.assertIsNone(self.menu([""])[0])

    def test_menu_free_text_and_the_limit(self):
        items = ["d%d" % i for i in range(pick.MENU_LIMIT + 5)]
        got, err = self.menu(["src/new"], items=items, free_text=True)
        self.assertEqual(got, "src/new")
        self.assertIn("  ... 5 more (type a value to use it)\n", err)
        self.assertNotIn("d%d" % pick.MENU_LIMIT, err)
        self.assertIn("... 5 more (install fzf to filter)", self.menu(["q"], items=items)[1])

    def test_nothing_to_choose_from(self):
        self.assertIsNone(pick.choose([], "host>"))


class Main(unittest.TestCase):
    def test_prints_selection(self):
        with mock.patch.object(pick, "pick", return_value=["beta"]), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(pick.main(["hosts", *ITEMS]), 0)
        self.assertEqual(out.getvalue(), "beta\n")

    def test_abort_exits_130(self):
        with mock.patch.object(pick, "pick", side_effect=pick.Abort):
            self.assertEqual(pick.main(["hosts", *ITEMS]), 130)

    def test_needs_noun_and_items(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(pick.main(["hosts"]), 2)
        self.assertIn("Missing argument 'ITEM...'", err.getvalue())

    def test_help_is_the_docstring_as_written(self):
        # Laid out like the other tools' help, not argparse's.
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(pick.main(["-h"]), 0)
        self.assertTrue(out.getvalue().startswith(pick.__doc__.rstrip() + "\n"), out.getvalue())


class PickFrom(unittest.TestCase):
    def test_twins_numbered_and_mapped_back(self):
        items = [("a", 1), ("b", 2), ("a", 3)]
        with mock.patch.object(pick, "pick", return_value=["a #2", "b"]) as p:
            chosen = pick.pick_from(items, "things", lambda t: t[0])
        p.assert_called_once_with(["a", "b", "a #2"], "things")
        self.assertEqual(chosen, [("b", 2), ("a", 3)])


if __name__ == "__main__":
    unittest.main()
