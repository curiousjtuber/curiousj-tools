import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from curiousj_tools import attrs, lists
from curiousj_tools.attrs import Condition, Term
from curiousj_tools.lists import Login, Operation, PathInfo, ToolError

TOML = '''# mine
logins = ["alice@devbox", "build.example.com"]

[[paths]]
path = "a"
git_url = "git@example.com:me/a.git"
git_branch = "main"

[[paths]]
path = "b"
'''

YAML = '''# mine
logins:
  - alice@devbox
  - login: build.example.com
    commands: [distrobox enter dev, exec zsh]
    via: "distrobox enter dev -- "
paths:
  - path: a
    git_url: git@example.com:me/a.git
    git_branch: main
  - b
'''

EXPECTED = lists.Lists(
    [Login("alice@devbox"), Login("build.example.com")],
    [PathInfo("a", "git@example.com:me/a.git", "main"), PathInfo("b")])


class Formats(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, text):
        path = os.path.join(self.tmp.name, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_toml(self):
        found = lists.load(self.write("ssh-lists.toml", TOML))
        self.assertEqual((found.logins, found.paths), (EXPECTED.logins, EXPECTED.paths))
        self.assertTrue(found.files[0].endswith("ssh-lists.toml"))
        self.assertEqual(found.logins[0].file, found.files[0])

    def test_yaml_with_tables_and_strings_mixed(self):
        for name in ("ssh-lists.yaml", "ssh-lists.yml"):
            found = lists.load(self.write(name, YAML))
            self.assertEqual(found.logins, [Login("alice@devbox"),
                                            Login("build.example.com",
                                                  ["distrobox enter dev", "exec zsh"],
                                                  "distrobox enter dev --")])
            self.assertEqual(found.paths, EXPECTED.paths)

    def test_json(self):
        data = {"logins": ["alice@devbox", {"login": "build.example.com"}],
                "paths": [{"path": "a", "git_url": "git@example.com:me/a.git",
                           "git_branch": "main"}, "b"]}
        found = lists.load(self.write("ssh-lists.json", json.dumps(data)))
        self.assertEqual((found.logins, found.paths), (EXPECTED.logins, EXPECTED.paths))

    def test_empty_documents_are_empty_lists(self):
        for name, text in (("e.toml", "# nothing\n"), ("e.yaml", ""), ("e.json", "{}")):
            found = lists.load(self.write(name, text))
            self.assertEqual((found.logins, found.paths), ([], []))
        found = lists.load(self.write("n.yaml", "logins:\npaths:\n"))
        self.assertEqual((found.logins, found.paths), ([], []))

    def test_unknown_extension_and_broken_files(self):
        with self.assertRaises(ToolError) as cm:
            lists.load(self.write("ssh-lists", "a\n"))
        self.assertIn("extension", str(cm.exception))
        for name, text in (("b.toml", "[[paths\n"), ("b.yaml", "a: [\n"), ("b.json", "{")):
            with self.assertRaises(ToolError) as cm:
                lists.load(self.write(name, text))
            self.assertIn(name, str(cm.exception))


class Parse(unittest.TestCase):
    def bad(self, data, msg):
        with self.assertRaises(ToolError) as cm:
            lists.parse(data, "F")
        self.assertIn(msg, str(cm.exception))
        self.assertTrue(str(cm.exception).startswith("F: "))

    def test_shape_errors_name_the_entry(self):
        self.bad([], "table")
        self.bad({"hosts": []}, "unknown key 'hosts'")
        self.bad({"logins": "a"}, "logins has to be a list")
        self.bad({"logins": [3]}, "logins entry 1: a string or a table")
        self.bad({"logins": [{"commands": ["x"]}]}, "logins entry 1: needs a login")
        self.bad({"logins": [{"login": "a", "user": "u"}]},
                 "unknown key 'user'; known: login, commands, via, attributes, operations")
        self.bad({"logins": [{"login": "a", "via": 3}]}, "logins entry 1 (a): via has to be a command line")
        self.bad({"logins": [{"login": "a", "via": " "}]}, "via has to be")
        self.bad({"logins": [{"login": "a", "commands": "x"}]}, "logins entry 1 (a): commands")
        self.bad({"logins": [{"login": "a", "commands": [""]}]}, "commands")
        self.bad({"paths": [{"url": "u"}]},
                 "paths entry 1: unknown key 'url'; known: path, git_url, git_branch, attributes, operations")
        self.bad({"paths": [{"path": ""}]}, "paths entry 1: needs a path")
        self.bad({"paths": [{"path": "p", "git_url": 3}]}, "paths entry 1 (p): git_url has to be a string")
        self.bad({"paths": [{"path": "p", "logins": 3}]}, "paths entry 1 (p): logins: a condition is")

    def test_bare_strings_and_null_commands(self):
        found = lists.parse({"logins": ["a", {"login": "b", "commands": None, "via": None}], "paths": ["p"]})
        self.assertEqual(found.logins, [Login("a"), Login("b")])
        self.assertEqual(found.paths, [PathInfo("p")])

    def test_a_path_names_the_logins_it_is_cloned_on(self):
        found = lists.parse({"paths": ["p", {"path": "q", "logins": {"any": ["dev", "mac"]}}]}, "F")
        self.assertEqual([p.logins for p in found.paths],
                         [attrs.EVERYTHING, attrs.condition({"any": ["dev", "mac"]}, "T")])

    def test_attributes_and_operations_ride_on_entries(self):
        found = lists.parse({
            "logins": [{"login": "a", "attributes": ["mise", "arch=x86_64"],
                        "operations": {"sys": " brew upgrade "}}],
            "paths": [{"path": "p", "attributes": ["git"], "operations": {"git-pull": "git pull"}}],
        }, "F")
        self.assertEqual(found.logins, [Login("a", attributes={"mise": None, "arch": "x86_64"},
                                              operations={"sys": "brew upgrade"})])
        self.assertEqual(found.paths, [PathInfo("p", attributes={"git": None},
                                                operations={"git-pull": "git pull"})])
        # names only entries define become operations without a command, once merged
        self.assertEqual(found.operations, {})
        # check() reports them and changes nothing; it used to add them itself
        self.assertEqual(lists.check(found), {"sys": "login", "git-pull": "path"})
        self.assertEqual(found.operations, {})
        self.assertEqual(lists.merge([found]).operations,
                         {"sys": Operation("sys"), "git-pull": Operation("git-pull", paths=attrs.EVERYTHING)})
        empty = lists.parse({"logins": [{"login": "a", "attributes": None, "operations": None}]})
        self.assertEqual(empty.logins, [Login("a")])

    def test_operations_table_parses_commands_groups_and_conditions(self):
        found = lists.parse({"operations": {
            "git-pull": {"command": "git pull", "paths": "git", "clone": True},
            "all-paths": {"command": "ls", "paths": True},
            "mise": {"command": " mise up ", "logins": ["mise", "arch!=arm64"], "serial": True},
            "brew": {"command": "brew up", "logins": {"any": ["mac", "brew"], "none": "headless"}},
            "sys": {"operations": ["mise", "brew"]},
            "all": {"operations": ["git-pull", "sys"]},
        }}, "F")
        ops = found.operations
        self.assertEqual(ops["git-pull"], Operation("git-pull", "git pull", paths=Condition(all=(Term("git"),)),
                                                    clone=True))
        self.assertEqual(ops["all-paths"], Operation("all-paths", "ls", paths=attrs.EVERYTHING))
        self.assertEqual(ops["mise"], Operation("mise", "mise up", serial=True,
                                                logins=Condition(all=(Term("mise"), Term("arch", "arm64", True)))))
        self.assertEqual(ops["brew"].logins, Condition(any=(Term("mac"), Term("brew")), none=(Term("headless"),)))
        self.assertEqual(ops["sys"], Operation("sys", members=["mise", "brew"]))
        self.assertTrue(ops["sys"].group and not ops["sys"].per_path)
        self.assertTrue(ops["git-pull"].per_path and not ops["git-pull"].group)
        self.assertFalse(ops["mise"].per_path)
        self.assertEqual(ops["all"].file, "F")
        self.assertEqual(lists.parse({"operations": None}).operations, {})

    def test_operation_shape_errors_name_the_operation(self):
        def bad_op(raw, msg):
            self.bad({"operations": {"x": raw}}, "operations 'x': " + msg)
        self.bad({"operations": []}, "operations has to be a table")
        bad_op("ls", "a table with command")
        bad_op({}, "either a command or operations")
        bad_op({"command": "ls", "operations": ["y"]}, "either a command or operations")
        bad_op({"command": ""}, "command has to be a command line")
        bad_op({"command": "ls", "url": "u"}, "unknown key 'url'")
        bad_op({"operations": []}, "operations has to be a list of operation names")
        bad_op({"operations": ["y"], "logins": "mise"}, "a group has nothing but its operations")
        bad_op({"command": "ls", "clone": True}, "clone goes with paths")
        bad_op({"command": "ls", "clone": "yes", "paths": True}, "clone has to be true or false")
        bad_op({"command": "ls", "serial": 1}, "serial has to be true or false")
        bad_op({"command": "ls", "paths": False}, "paths is a condition, or true")
        bad_op({"command": "ls", "paths": "=v"}, "paths: '=v'")
        bad_op({"command": "ls", "logins": {"some": "x"}}, "logins: unknown key 'some'")
        self.bad({"operations": {3: {"command": "ls"}}}, "a name has to be a word")

    def test_per_entry_operations_shape_errors(self):
        self.bad({"logins": [{"login": "a", "operations": ["x"]}]},
                 "logins entry 1 (a): operations has to be a table of name = command")
        self.bad({"logins": [{"login": "a", "operations": {"x": ""}}]},
                 "logins entry 1 (a): operations 'x' has to be a command line")
        self.bad({"paths": [{"path": "p", "operations": {"": "ls"}}]},
                 "paths entry 1 (p): operations has to be a table")
        self.bad({"paths": [{"path": "p", "attributes": ["a", "a"]}]},
                 "paths entry 1 (p): attribute 'a' given twice")
        self.bad({"logins": [{"login": "a", "attributes": "mise"}]},
                 "logins entry 1 (a): attributes has to be a list")


class FindFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"HOME": self.tmp.name}

    def write(self, rel, text="logins = ['h1']\n"):
        path = os.path.join(self.tmp.name, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_explicit_files_in_order_each_readable(self):
        a, b = self.write("a.toml"), self.write("b.yaml")
        self.write(".config/ssh-lists/ssh-lists.toml")
        self.assertEqual(lists.find_files([b, a], self.env), [b, a])
        with self.assertRaises(ToolError) as cm:
            lists.find_files([a, os.path.join(self.tmp.name, "none")], self.env)
        self.assertIn("cannot read", str(cm.exception))
        self.assertNotIn("$SSH_LISTS_FILE", str(cm.exception))

    def test_file_var_is_colon_separated_then_the_search_path(self):
        xdg = self.write(".config/ssh-lists/ssh-lists.toml")
        self.assertEqual(lists.find_files((), self.env), [xdg])
        in_dir = self.write("lists/ssh-lists.yaml")
        env = dict(self.env, SSH_LISTS_PATH=os.path.join(self.tmp.name, "nowhere")
                   + ":" + os.path.join(self.tmp.name, "lists"))
        self.assertEqual(lists.find_files((), env), [in_dir])
        one, two = self.write("via-var.json"), self.write("two.yml")
        env["SSH_LISTS_FILE"] = one + ":" + two + ":"
        self.assertEqual(lists.find_files((), env), [one, two])
        env["SSH_LISTS_FILE"] = os.path.join(self.tmp.name, "gone.json")
        with self.assertRaises(ToolError) as cm:
            lists.find_files((), env)
        self.assertIn("$SSH_LISTS_FILE", str(cm.exception))

    def test_every_matching_file_in_every_directory_sorted_by_name(self):
        main = self.write("a/ssh-lists.yaml")
        local = self.write("a/ssh-lists-local.yaml")
        more = self.write("a/ssh-lists.toml")
        other = self.write("b/ssh-lists.json")
        any_name = self.write("a/lists.toml")
        extra = self.write("b/extra.yaml")
        for name in ("a/ssh-lists.toml~", "a/ssh-lists.yaml.~1~", "a/notes.txt",
                     "b/ssh-lists-x.md"):
            self.write(name)
        os.makedirs(os.path.join(self.tmp.name, "a", "ssh-lists.d.toml"))
        env = dict(self.env, SSH_LISTS_PATH=os.path.join(self.tmp.name, "a") + ":"
                   + os.path.join(self.tmp.name, "b"))
        self.assertEqual(lists.find_files((), env), [any_name, local, more, main, extra, other])

    def test_default_is_a_directory_of_its_own(self):
        cfg = self.write(".config/ssh-lists/mine.yaml")
        self.write(".config/other.toml")
        self.write(".config/ssh-lists.toml")
        self.assertEqual(lists.find_files((), self.env), [cfg])

    def test_search_path_replaces_the_default(self):
        self.write(".config/ssh-lists/ssh-lists.toml")
        env = dict(self.env, SSH_LISTS_PATH=os.path.join(self.tmp.name, "nowhere"))
        with self.assertRaises(ToolError) as cm:
            lists.find_files((), env)
        self.assertIn("SSH_LISTS_FILE", str(cm.exception))
        self.assertIn("SSH_LISTS_PATH", str(cm.exception))

    def test_xdg_config_home_honoured(self):
        cfg = self.write("xdg/ssh-lists/ssh-lists.json", "{}")
        env = dict(self.env, XDG_CONFIG_HOME=os.path.join(self.tmp.name, "xdg"))
        self.assertEqual(lists.find_files((), env), [cfg])
        self.assertEqual(lists.load_all((), env).files, [cfg])


class Merge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, text):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def load(self, *texts):
        paths = [self.write(f"{n}.yaml", t) for n, t in enumerate(texts)]
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            found = lists.load_files(paths)
        return found, err.getvalue()

    def test_later_files_add_and_first_definition_wins_with_a_warning(self):
        found, err = self.load(
            "logins: [a]\npaths: [p]\noperations: {x: {command: one}}\n",
            "logins: [b, a]\npaths: [q, p]\noperations: {y: {command: two}, x: {command: three}}\n")
        self.assertEqual([e.login for e in found.logins], ["a", "b"])
        self.assertEqual([p.path for p in found.paths], ["p", "q"])
        self.assertEqual({n: op.command for n, op in found.operations.items()}, {"x": "one", "y": "two"})
        self.assertEqual(found.files, [os.path.join(self.tmp.name, "0.yaml"), os.path.join(self.tmp.name, "1.yaml")])
        self.assertEqual(err.splitlines(), [
            f"ssh-lists: duplicate login a in {found.files[1]}; keeping the one in {found.files[0]}",
            f"ssh-lists: duplicate path p in {found.files[1]}; keeping the one in {found.files[0]}",
            f"ssh-lists: duplicate operation x in {found.files[1]}; keeping the one in {found.files[0]}"])

    def test_duplicate_within_one_file_warns_too(self):
        found, err = self.load("logins: [a, a]\n")
        self.assertEqual(found.logins, [Login("a")])
        self.assertIn("duplicate login a", err)

    def test_twins_differing_in_commands_or_via_are_kept(self):
        found, err = self.load("logins:\n  - a\n  - {login: a, commands: [exec zsh]}\n"
                               "  - {login: a, via: 'env X=1'}\n")
        self.assertEqual(len(found.logins), 3)
        self.assertEqual(err, "")

    def test_group_members_and_overrides_may_come_from_another_file(self):
        found, err = self.load(
            "logins: [{login: a, operations: {sys: brew}}]\npaths: [{path: p, operations: {gp: git pull}}]\n"
            "operations: {all: {operations: [gp, sys]}}\n",
            "operations:\n  gp: {command: git pull -r, paths: git}\n  sys: {command: up, logins: mise}\n")
        self.assertEqual(err, "")
        self.assertEqual(found.operations["all"].members, ["gp", "sys"])
        self.assertEqual(found.operations["gp"].command, "git pull -r")

    def test_unknown_member_and_cycle_are_errors(self):
        with self.assertRaises(ToolError) as cm:
            self.load("operations: {all: {operations: [gp]}}\n")
        self.assertIn("operations 'all': unknown member 'gp'", str(cm.exception))
        with self.assertRaises(ToolError) as cm:
            self.load("operations:\n  a: {operations: [b]}\n  b: {operations: [c]}\n  c: {operations: [a]}\n")
        self.assertIn("a cycle, a -> b -> c -> a", str(cm.exception))
        with self.assertRaises(ToolError) as cm:
            self.load("operations: {a: {operations: [a]}}\n")
        self.assertIn("a cycle, a -> a", str(cm.exception))

    def test_scope_mismatch_is_an_error(self):
        cases = [
            ("logins: [{login: a, operations: {gp: x}}]\noperations: {gp: {command: c, paths: true}}\n",
             "logins entry (a): operations 'gp' runs per path, not per login"),
            ("paths: [{path: p, operations: {up: x}}]\noperations: {up: {command: c}}\n",
             "paths entry (p): operations 'up' runs per login, not per path"),
            ("paths: [{path: p, operations: {all: x}}]\noperations: {all: {operations: [b]}, b: {command: c}}\n",
             "paths entry (p): operations 'all' is a group"),
            ("logins: [{login: a, operations: {z: x}}]\npaths: [{path: p, operations: {z: y}}]\n",
             "paths entry (p): operations 'z' is a login's operation elsewhere"),
        ]
        for text, msg in cases:
            with self.assertRaises(ToolError, msg=text) as cm:
                self.load(text)
            self.assertIn(msg, str(cm.exception))

    def test_per_entry_only_names_become_implicit_operations(self):
        found, _ = self.load("logins: [{login: a, operations: {up: x}}]\n"
                             "paths: [{path: p, operations: {gp: y}}]\n")
        self.assertEqual(found.operations, {"up": Operation("up"), "gp": Operation("gp", paths=attrs.EVERYTHING)})
        self.assertIsNone(found.operations["up"].command)


class Builtins(unittest.TestCase):
    """The operations the package ships, read ahead of the lists files."""

    NAMES = ["git-pull", "uv-tool-update", "mise-update", "cachy-update", "brew-upgrade",
             "apt-upgrade", "distrobox-upgrade", "system-update", "update-all"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def load(self, *texts):
        paths = []
        for n, text in enumerate(texts):
            paths.append(os.path.join(self.tmp.name, f"{n}.yaml"))
            with open(paths[-1], "w") as f:
                f.write(text)
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            found = lists.load_all(paths, {"HOME": self.tmp.name})
        return found, err.getvalue()

    def test_the_shipped_file_is_operations_only_and_consistent(self):
        builtin = lists.builtin_operations()
        self.assertEqual(list(builtin.operations), self.NAMES)
        self.assertEqual((builtin.logins, builtin.paths), ([], []))
        self.assertEqual(list(lists.merge([], builtin).operations), self.NAMES)

    def test_read_first_and_not_counted_among_the_files(self):
        found, err = self.load("operations: {mine: {command: x}}\n")
        self.assertEqual(err, "")
        self.assertEqual(list(found.operations), self.NAMES + ["mine"])
        self.assertEqual(found.files, [os.path.join(self.tmp.name, "0.yaml")])
        self.assertTrue(found.operations["git-pull"].file.endswith("operations.toml"))

    def test_a_file_replaces_one_in_place_without_a_warning(self):
        found, err = self.load("operations: {git-pull: {command: git pull --ff-only, paths: git}}\n")
        self.assertEqual(err, "")
        self.assertEqual(list(found.operations), self.NAMES)
        self.assertEqual(found.operations["git-pull"].command, "git pull --ff-only")
        self.assertFalse(found.operations["git-pull"].clone)

    def test_two_files_defining_one_still_warn(self):
        found, err = self.load("operations: {git-pull: {command: one, paths: git}}\n",
                               "operations: {git-pull: {command: two, paths: git}}\n")
        self.assertEqual(found.operations["git-pull"].command, "one")
        self.assertIn("duplicate operation git-pull", err)

    def test_groups_and_entries_use_them(self):
        found, err = self.load("paths: [{path: p, operations: {git-pull: git pull --ff-only}}]\n"
                               "operations: {nightly: {operations: [update-all, mine]}, "
                               "mine: {command: x}}\n")
        self.assertEqual(err, "")
        self.assertEqual(found.operations["nightly"].members, ["update-all", "mine"])
        with self.assertRaises(ToolError) as cm:
            self.load("paths: [{path: p, operations: {mise-update: x}}]\n")
        self.assertIn("operations 'mise-update' runs per login, not per path", str(cm.exception))


class ExampleFiles(unittest.TestCase):
    """The shipped examples parse, and say the same thing in each format."""

    ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")

    def load(self, *names):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            found = lists.load_all([os.path.join(self.ROOT, n) for n in names], {})
        self.assertEqual(err.getvalue(), "")
        return found

    def test_examples_agree(self):
        toml, yaml = self.load("ssh-lists.toml"), self.load("ssh-lists.yaml")
        self.assertEqual((toml.logins, toml.paths, toml.operations),
                         (yaml.logins, yaml.paths, yaml.operations))
        self.assertTrue(toml.logins and toml.paths)
        self.assertTrue(set(toml.operations) > set(Builtins.NAMES))

    def test_operations_file_adds_to_the_yaml_without_a_warning(self):
        found = self.load("ssh-lists.yaml", "operations.yaml")
        alone = self.load("ssh-lists.yaml")
        self.assertTrue(set(alone.operations) < set(found.operations))
        group = next(op for op in found.operations.values()
                     if op.group and op.file.endswith("operations.yaml"))
        files = {found.operations[m].file.rsplit("/", 1)[1] for m in group.members}
        self.assertEqual(files, {"operations.yaml", "ssh-lists.yaml", "operations.toml"})
        with self.assertRaises(ToolError) as cm:  # the group needs the other file
            self.load("operations.yaml")
        self.assertIn("unknown member 'test'", str(cm.exception))



class ReadmeBlocks(unittest.TestCase):
    """The README's lists excerpts, which repeat what the examples and the
    docstrings say, stay valid: each parses and checks as a lists file on
    its own, and the YAML says what the TOML before it does."""

    def blocks(self):
        readme = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "README.md")
        text = open(readme, encoding="utf-8").read()
        found = []
        for lang in ("toml", "yaml"):
            for chunk in text.split("```" + lang + "\n")[1:]:
                found.append((lang, chunk.split("```", 1)[0]))
        return found

    def parsed(self, lang, body):
        from ruamel.yaml import YAML
        import tomllib
        data = tomllib.loads(body) if lang == "toml" else YAML(typ="safe").load(body)
        return lists.merge([lists.parse(data, f"README {lang} block")], lists.builtin_operations())

    def test_every_block_is_a_valid_lists_file(self):
        blocks = self.blocks()
        self.assertGreaterEqual(len(blocks), 4)
        for lang, body in blocks:
            with self.subTest(block=body[:40]):
                self.parsed(lang, body)

    def test_the_yaml_says_what_the_toml_does(self):
        # The first of each: the README's "The same in YAML" pair.
        first = {}
        for lang, body in self.blocks():
            first.setdefault(lang, body)
        toml, yaml = self.parsed("toml", first["toml"]), self.parsed("yaml", first["yaml"])
        self.assertEqual((toml.logins, toml.paths), (yaml.logins, yaml.paths))


if __name__ == "__main__":
    unittest.main()
