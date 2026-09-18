import json
import os
import subprocess
import tempfile
import unittest

from curiousj_tools import lists
from curiousj_tools.lists import HostInfo, HostsError, PathInfo

TOML = '''# mine
hosts = ["alice@devbox", "build.example.com"]

[[paths]]
path = "a"
git_url = "git@example.com:me/a.git"
git_branch = "main"

[[paths]]
path = "b"
'''

YAML = '''# mine
hosts:
  - alice@devbox
  - host: build.example.com
    commands: [distrobox enter dev, exec zsh]
paths:
  - path: a
    git_url: git@example.com:me/a.git
    git_branch: main
  - b
'''

EXPECTED = lists.Lists(
    [HostInfo("alice@devbox"), HostInfo("build.example.com")],
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
        self.assertEqual((found.hosts, found.paths), (EXPECTED.hosts, EXPECTED.paths))
        self.assertTrue(found.file.endswith("ssh-lists.toml"))

    def test_yaml_with_tables_and_strings_mixed(self):
        for name in ("ssh-lists.yaml", "ssh-lists.yml"):
            found = lists.load(self.write(name, YAML))
            self.assertEqual(found.hosts, [HostInfo("alice@devbox"),
                                           HostInfo("build.example.com",
                                                    ["distrobox enter dev", "exec zsh"])])
            self.assertEqual(found.paths, EXPECTED.paths)

    def test_json(self):
        data = {"hosts": ["alice@devbox", {"host": "build.example.com"}],
                "paths": [{"path": "a", "git_url": "git@example.com:me/a.git",
                           "git_branch": "main"}, "b"]}
        found = lists.load(self.write("ssh-lists.json", json.dumps(data)))
        self.assertEqual((found.hosts, found.paths), (EXPECTED.hosts, EXPECTED.paths))

    def test_empty_documents_are_empty_lists(self):
        for name, text in (("e.toml", "# nothing\n"), ("e.yaml", ""), ("e.json", "{}")):
            found = lists.load(self.write(name, text))
            self.assertEqual((found.hosts, found.paths), ([], []))
        found = lists.load(self.write("n.yaml", "hosts:\npaths:\n"))
        self.assertEqual((found.hosts, found.paths), ([], []))

    def test_unknown_extension_and_broken_files(self):
        with self.assertRaises(HostsError) as cm:
            lists.load(self.write("ssh-lists", "a\n"))
        self.assertIn("extension", str(cm.exception))
        for name, text in (("b.toml", "[[paths\n"), ("b.yaml", "a: [\n"), ("b.json", "{")):
            with self.assertRaises(HostsError) as cm:
                lists.load(self.write(name, text))
            self.assertIn(name, str(cm.exception))


class Parse(unittest.TestCase):
    def bad(self, data, msg):
        with self.assertRaises(HostsError) as cm:
            lists.parse(data, "F")
        self.assertIn(msg, str(cm.exception))
        self.assertTrue(str(cm.exception).startswith("F: "))

    def test_shape_errors_name_the_entry(self):
        self.bad([], "table")
        self.bad({"dirs": []}, "unknown key 'dirs'")
        self.bad({"hosts": "a"}, "hosts has to be a list")
        self.bad({"hosts": [3]}, "hosts entry 1: a string or a table")
        self.bad({"hosts": [{"commands": ["x"]}]}, "hosts entry 1: needs a host")
        self.bad({"hosts": [{"host": "a", "user": "u"}]}, "unknown key 'user'; known: host, commands")
        self.bad({"hosts": [{"host": "a", "commands": "x"}]}, "hosts entry 1 (a): commands")
        self.bad({"hosts": [{"host": "a", "commands": [""]}]}, "commands")
        self.bad({"paths": [{"url": "u"}]}, "paths entry 1: unknown key 'url'; known: path, git_url, git_branch")
        self.bad({"paths": [{"path": ""}]}, "paths entry 1: needs a path")
        self.bad({"paths": [{"path": "p", "git_url": 3}]}, "paths entry 1 (p): git_url has to be a string")

    def test_bare_strings_and_null_commands(self):
        found = lists.parse({"hosts": ["a", {"host": "b", "commands": None}], "paths": ["p"]})
        self.assertEqual(found.hosts, [HostInfo("a"), HostInfo("b")])
        self.assertEqual(found.paths, [PathInfo("p")])


class FindFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"HOME": self.tmp.name}

    def write(self, rel, text="hosts = ['h1']\n"):
        path = os.path.join(self.tmp.name, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_explicit_wins_and_has_to_be_readable(self):
        p = self.write("explicit.toml")
        self.write(".config/ssh-lists.toml")
        self.assertEqual(lists.find_file(p, self.env), p)
        with self.assertRaises(HostsError) as cm:
            lists.find_file(os.path.join(self.tmp.name, "none"), self.env)
        self.assertIn("cannot read", str(cm.exception))

    def test_file_var_then_search_path_then_config_home(self):
        xdg = self.write(".config/ssh-lists.toml")
        self.assertEqual(lists.find_file(None, self.env), xdg)
        in_dir = self.write("lists/ssh-lists.yaml")
        env = dict(self.env, SSH_LISTS_PATH=os.path.join(self.tmp.name, "nowhere")
                   + ":" + os.path.join(self.tmp.name, "lists"))
        self.assertEqual(lists.find_file(None, env), in_dir)
        via_var = self.write("via-var.json")
        env["SSH_LISTS_FILE"] = via_var
        self.assertEqual(lists.find_file(None, env), via_var)
        env["SSH_LISTS_FILE"] = os.path.join(self.tmp.name, "gone.json")
        with self.assertRaises(HostsError) as cm:
            lists.find_file(None, env)
        self.assertIn("$SSH_LISTS_FILE", str(cm.exception))

    def test_search_path_replaces_the_default(self):
        self.write(".config/ssh-lists.toml")
        env = dict(self.env, SSH_LISTS_PATH=os.path.join(self.tmp.name, "nowhere"))
        with self.assertRaises(HostsError) as cm:
            lists.find_file(None, env)
        self.assertIn("SSH_LISTS_FILE", str(cm.exception))
        self.assertIn("SSH_LISTS_PATH", str(cm.exception))

    def test_suffix_order_within_a_directory(self):
        yml = self.write(".config/ssh-lists.yml")
        self.assertEqual(lists.find_file(None, self.env), yml)
        yaml = self.write(".config/ssh-lists.yaml")
        self.assertEqual(lists.find_file(None, self.env), yaml)
        toml = self.write(".config/ssh-lists.toml")
        self.assertEqual(lists.find_file(None, self.env), toml)

    def test_xdg_config_home_honoured(self):
        cfg = self.write("xdg/ssh-lists.json", "{}")
        env = dict(self.env, XDG_CONFIG_HOME=os.path.join(self.tmp.name, "xdg"))
        self.assertEqual(lists.find_file(None, env), cfg)


class ExampleFiles(unittest.TestCase):
    """The shipped examples parse, and say the same thing in each format."""

    def test_examples_agree(self):
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
        toml = lists.load(os.path.join(root, "ssh-lists.toml"))
        yaml = lists.load(os.path.join(root, "ssh-lists.yaml"))
        self.assertEqual((toml.hosts, toml.paths), (yaml.hosts, yaml.paths))
        self.assertTrue(toml.hosts and toml.paths)


if __name__ == "__main__":
    unittest.main()
