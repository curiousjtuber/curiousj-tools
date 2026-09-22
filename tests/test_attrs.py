import unittest

from curiousj_tools import attrs
from curiousj_tools.attrs import Condition, Term

MISE = {"mise": None, "arch": "x86_64"}


class Terms(unittest.TestCase):
    def test_the_four_forms(self):
        self.assertEqual(attrs.term("mise", "W"), Term("mise"))
        self.assertEqual(attrs.term("arch=x86_64", "W"), Term("arch", "x86_64"))
        self.assertEqual(attrs.term("!mise", "W"), Term("mise", negated=True))
        self.assertEqual(attrs.term("arch!=arm64", "W"), Term("arch", "arm64", True))
        self.assertEqual(attrs.term(" mise ", "W"), Term("mise"))

    def test_holds(self):
        self.assertTrue(Term("mise").holds(MISE))
        self.assertFalse(Term("brew").holds(MISE))
        self.assertTrue(Term("arch", "x86_64").holds(MISE))
        self.assertFalse(Term("arch", "arm64").holds(MISE))
        self.assertFalse(Term("mise", negated=True).holds(MISE))
        self.assertTrue(Term("brew", negated=True).holds(MISE))
        self.assertTrue(Term("arch", "arm64", True).holds(MISE))
        self.assertTrue(Term("os", "mac", True).holds(MISE))
        self.assertFalse(Term("arch", "x86_64", True).holds(MISE))

    def test_a_flag_does_not_equal_a_value(self):
        self.assertFalse(Term("mise", "1").holds(MISE))
        self.assertTrue(Term("arch").holds(MISE))

    def test_bad_terms_name_the_place(self):
        for text in ("", " ", "=v", "a==b", "!a=b", "a b", "a=", "!", "a!b", "!!a", "a!!=b", 3, None):
            with self.assertRaises(ValueError, msg=repr(text)) as cm:
                attrs.term(text, "F: operations 'x'")
            self.assertTrue(str(cm.exception).startswith("F: operations 'x': "), str(cm.exception))

    def test_str_round_trips(self):
        for text in ("mise", "arch=x86_64", "!mise", "arch!=arm64"):
            self.assertEqual(str(attrs.term(text, "W")), text)


class Conditions(unittest.TestCase):
    def test_string_list_and_table_forms_agree(self):
        one = attrs.condition("mise", "W")
        self.assertEqual(one, Condition(all=(Term("mise"),)))
        self.assertEqual(attrs.condition(["mise"], "W"), one)
        self.assertEqual(attrs.condition({"all": "mise"}, "W"), one)
        self.assertEqual(attrs.condition({"all": ["mise"]}, "W"), one)

    def test_true_and_empty_match_everything(self):
        for raw in (True, None, [], {}):
            cond = attrs.condition(raw, "W")
            self.assertEqual(cond, attrs.EVERYTHING, raw)
            self.assertTrue(cond.matches({}))
            self.assertTrue(cond.matches(MISE))
        self.assertEqual(str(attrs.EVERYTHING), "everything")

    def test_all_any_none_together(self):
        cond = attrs.condition({"all": "mise", "any": ["mac", "brew"], "none": "headless"}, "W")
        self.assertTrue(cond.matches({"mise": None, "brew": None}))
        self.assertFalse(cond.matches({"brew": None}))                    # all fails
        self.assertFalse(cond.matches({"mise": None}))                    # any fails
        self.assertFalse(cond.matches({"mise": None, "mac": None, "headless": None}))
        self.assertEqual(str(cond), "mise; any mac brew; none headless")
        self.assertEqual(str(attrs.condition(["mise", "arch!=arm64"], "W")), "mise arch!=arm64")
        self.assertTrue(attrs.condition(["mise", "arch=x86_64"], "W").matches(MISE))
        self.assertFalse(attrs.condition(["mise", "arch=arm64"], "W").matches(MISE))

    def test_bad_shapes(self):
        for raw in (False, 3, {"some": "mise"}, [3], {"all": 3}):
            with self.assertRaises(ValueError, msg=repr(raw)) as cm:
                attrs.condition(raw, "F: x")
            self.assertTrue(str(cm.exception).startswith("F: x: "))

    def test_holds_all_is_every_term(self):
        terms = (Term("mise"), Term("arch", "arm64", True))
        self.assertTrue(attrs.holds_all(terms, MISE))
        self.assertFalse(attrs.holds_all(terms, {"mise": None, "arch": "arm64"}))
        self.assertTrue(attrs.holds_all((), {}))


class Attributes(unittest.TestCase):
    def test_flags_and_values_become_a_dict(self):
        self.assertEqual(attrs.attributes(["mise", "arch=x86_64", " git "], "W"),
                         {"mise": None, "arch": "x86_64", "git": None})
        self.assertEqual(attrs.attributes(None, "W"), {})
        self.assertEqual(attrs.attributes([], "W"), {})

    def test_duplicate_key_and_bad_items_are_errors(self):
        for raw in ("mise", {"mise": True}, [3], [""], ["=v"], ["a=b=c"], ["a="], ["!a"],
                    ["a b"], ["mise", "mise"], ["arch=1", "arch=2"]):
            with self.assertRaises(ValueError, msg=repr(raw)) as cm:
                attrs.attributes(raw, "F: logins entry 1 (a)")
            self.assertTrue(str(cm.exception).startswith("F: logins entry 1 (a): "))


if __name__ == "__main__":
    unittest.main()
