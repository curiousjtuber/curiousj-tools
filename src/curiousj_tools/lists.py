"""The lists behind ssh-logins, xssh and pssh: logins to reach, paths to work in, operations to run.

TOML, YAML or JSON, told apart by the extension: ssh-lists.toml, .yaml, .yml
or .json. Three parts, all optional:

    logins = [
        "alice@devbox",                     # [user@]host, as ssh takes it
        { login = "build.example.com", commands = ["distrobox enter dev -nw"],
          via = "distrobox enter dev -nw --", attributes = ["mise", "cachyos"] },
    ]   # commands: run after login, in the pane xssh opens
        # via: what pssh runs its command through there, given `sh -c `...'`
        # attributes: flags and key=value pairs the operations select on;
        #   'shared-home', for a container sharing the host's home, keeps
        #   pssh and xssh from working on its paths a second time

    [[paths]]
    path = "src/webapp"                     # relative to ~ unless absolute
    git_url = "git@github.com:me/webapp.git"    # optional: what `pssh -c` clones it from
    git_branch = "main"                     # optional: -b for that clone
    attributes = ["git", "py-project"]
    logins = "dev"                          # optional: the logins it is cloned on; elsewhere
                                            # it is worked on only where it already is

    [operations.git-pull]                   # `pssh -o git-pull`
    command = "git pull --rebase --autostash"
    paths = "git"                           # per path: in every path matching; true for all
    clone = true                            # a missing path is cloned first, as `pssh -c`

    [operations.mise-update]
    command = "mise self-update -y && mise upgrade"
    logins = "mise"                         # per login, on those matching; absent: every login
    serial = true                           # asks questions: pssh runs it one login at a time

    [operations.system-update]              # a group: each login runs the members it matches
    operations = ["cachy-update", "brew-upgrade"]

A login is a string or a table ('[[logins]]' tables work too, when none is
a bare string); so is a path. A condition ('logins', 'paths') is a term, a
list of terms that all have to hold, or a table with 'all', 'any', 'none';
a term is 'key', 'key=value', '!key' or 'key!=value' (see curiousj_tools.attrs).
A login or path can carry its own command for an operation, which is then
run there whatever the operation's condition says, and can name an
operation no [operations.*] table defines:

    [[paths]]
    path = "src/legacy"
    operations = { git-pull = "git pull --ff-only" }

In YAML the same reads:

    logins:
      - alice@devbox
      - login: build.example.com
        commands: [distrobox enter dev -nw]
        via: distrobox enter dev -nw --
        attributes: [mise, cachyos]
    paths:
      - path: src/webapp
        git_url: git@github.com:me/webapp.git
        attributes: [git]
    operations:
      git-pull:
        command: git pull --rebase --autostash
        paths: git
        clone: true

Several files make one list. Which are read:

    -f FILE                     on the command line, repeatable: those and no other
    $SSH_LISTS_FILE             colon-separated files, when no -f
    $SSH_LISTS_PATH             colon-separated directories, otherwise: every
                                .toml, .yaml, .yml or .json file in each, by
                                name; default $XDG_CONFIG_HOME/ssh-lists
                                (~/.config/ssh-lists)

Files merge in that order, those of a directory by name. An entry given
twice -- a login with the same commands and via, a path, an operation
name -- keeps its first definition, with a warning naming both files.

Ahead of them all come the operations curiousj-tools ships, in
'operations.toml' beside this module: git-pull, uv-tool-update, mise-update,
cachy-update, brew-upgrade, apt-upgrade, system-update and update-all
(`pssh -L` lists them). A file's operation of the same name takes a
built-in's place, without a warning.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Sequence

from . import attrs
from .attrs import EVERYTHING, Condition

NAME = "ssh-lists"
SUFFIXES = (".toml", ".yaml", ".yml", ".json")
FILE_VAR = "SSH_LISTS_FILE"
PATH_VAR = "SSH_LISTS_PATH"
BUILTIN = "operations.toml"


class ToolError(Exception):
    """A user-facing failure: the message is printed and the tool exits 1."""


@dataclass
class Login:
    """One login, [user@]host as ssh takes it, and what to run once logged in."""

    login: str
    commands: list[str] = field(default_factory=list)
    via: str | None = None
    attributes: dict[str, str | None] = field(default_factory=dict)
    operations: dict[str, str] = field(default_factory=dict)  # name -> its own command
    file: str = field(default="", compare=False, repr=False)


@dataclass
class PathInfo:
    """One directory pssh works in, and where a missing one is cloned from."""

    path: str
    git_url: str | None = None
    git_branch: str | None = None
    attributes: dict[str, str | None] = field(default_factory=dict)
    operations: dict[str, str] = field(default_factory=dict)
    logins: Condition = EVERYTHING  # where it is cloned when missing
    file: str = field(default="", compare=False, repr=False)


@dataclass
class Operation:
    """A named command and where it applies, or a group of names. One with
    no command is a name only entries define, with their own commands."""

    name: str
    command: str | None = None
    members: list[str] = field(default_factory=list)
    logins: Condition = EVERYTHING
    paths: Condition | None = None  # set: per path, in the paths matching
    clone: bool = False
    serial: bool = False
    file: str = field(default="", compare=False, repr=False)

    @property
    def group(self) -> bool:
        return bool(self.members)

    @property
    def per_path(self) -> bool:
        return self.paths is not None


@dataclass
class Lists:
    """The files merged: their three parts and where they were read from."""

    logins: list[Login] = field(default_factory=list)
    paths: list[PathInfo] = field(default_factory=list)
    operations: dict[str, Operation] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)


def warn(message: str) -> None:
    print(f"{NAME}: {message}", file=sys.stderr)


def search_path(env: Any = None) -> list[str]:
    """The directories whose lists files are read: $SSH_LISTS_PATH, else
    ssh-lists in the XDG config home."""
    env = os.environ if env is None else env
    raw = env.get(PATH_VAR)
    if raw:
        return [os.path.expanduser(d) for d in raw.split(":") if d]
    home = env.get("HOME") or os.path.expanduser("~")
    return [os.path.join(env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), NAME)]


def find_files(explicit: Sequence[str] = (), env: Any = None) -> list[str]:
    """The lists files to read, in merge order; see the module docstring.
    Raises ToolError when a named file is unreadable or none is found."""
    env = os.environ if env is None else env
    if explicit:
        return [readable(f, "") for f in explicit]
    named = env.get(FILE_VAR)
    if named:
        return [readable(f, f" (${FILE_VAR})") for f in named.split(":") if f]
    dirs = search_path(env)
    found = []
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for name in names:
            path = os.path.join(d, name)
            if (os.path.splitext(name)[1].lower() in SUFFIXES
                    and os.path.isfile(path) and os.access(path, os.R_OK)):
                found.append(path)
    if not found:
        raise ToolError(
            f"no lists file ({'/'.join(SUFFIXES)}) in {':'.join(dirs)}: set ${FILE_VAR} or "
            f"${PATH_VAR}, or create ~/.config/{NAME}/{NAME}.toml (or .yaml, .yml, .json)")
    return found


def readable(path: str, note: str) -> str:
    if not os.access(path, os.R_OK):
        raise ToolError(f"cannot read {path}{note}")
    return path


def read_data(path: str) -> Any:
    """The file decoded by its extension. Raises ToolError on a format the
    name does not tell, or a file that does not parse."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in SUFFIXES:
        raise ToolError(f"{path}: the extension has to say the format: one of "
                        + ", ".join(SUFFIXES))
    try:
        if ext == ".toml":
            with open(path, "rb") as f:
                return tomllib.load(f)
        if ext == ".json":
            with open(path) as f:
                return json.load(f)
        return read_yaml(path)
    except (tomllib.TOMLDecodeError, json.JSONDecodeError) as e:
        raise ToolError(f"{path}: {e}") from None


def read_yaml(path: str) -> Any:
    # Imported here so the TOML and JSON readers, and everything that only
    # lists hosts, start without loading a YAML library.
    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError
    try:
        with open(path) as f:
            return YAML(typ="safe").load(f)
    except YAMLError as e:
        raise ToolError(f"{path}: {str(e).splitlines()[0]}") from None


def load(path: str) -> Lists:
    """One file, checked: every entry the shape the docstring shows, and
    the operations consistent. Raises ToolError naming the entry otherwise."""
    return load_files([path])


def load_files(paths: Sequence[str]) -> Lists:
    """The files merged, in order; see the module docstring."""
    return merge([parse(read_data(p), p) for p in paths])


def load_all(explicit: Sequence[str] = (), env: Any = None) -> Lists:
    """The files find_files finds, merged over the built-in operations."""
    return merge([parse(read_data(p), p) for p in find_files(explicit, env)],
                 builtin_operations())


def builtin_operations() -> Lists:
    """The operations curiousj-tools ships; see the module docstring."""
    source = resources.files(__package__) / BUILTIN
    return parse(tomllib.loads(source.read_text()), str(source))


def parse(data: Any, path: str = NAME) -> Lists:
    """One file's contents, each entry checked on its own; merge() does the
    checks between entries."""
    if data is None:  # an empty YAML document
        data = {}
    if not isinstance(data, dict):
        raise ToolError(f"{path}: the file has to be a table (mapping) with logins, paths "
                        "and operations")
    for key in data:
        if key not in ("logins", "paths", "operations"):
            raise ToolError(f"{path}: unknown key {key!r}; the parts are logins, paths "
                            "and operations")
    logins = [login_entry(e, n, path) for n, e in enumerate(entries(data, "logins", path), 1)]
    paths = [path_entry(e, n, path) for n, e in enumerate(entries(data, "paths", path), 1)]
    raw_ops = data.get("operations")
    if raw_ops is None:
        raw_ops = {}
    if not isinstance(raw_ops, dict):
        raise ToolError(f"{path}: operations has to be a table of name = {{ command = ... }}")
    operations = {name: operation_entry(name, raw, path) for name, raw in raw_ops.items()}
    return Lists(logins, paths, operations, [path])


def entries(data: dict, key: str, path: str) -> list:
    found = data.get(key, [])
    if found is None:
        return []
    if not isinstance(found, list):
        raise ToolError(f"{path}: {key} has to be a list")
    return found


LOGIN_KEYS = ("login", "commands", "via", "attributes", "operations")
PATH_KEYS = ("path", "git_url", "git_branch", "attributes", "operations", "logins")


def login_entry(raw: Any, n: int, path: str) -> Login:
    fields = table(raw, "login", LOGIN_KEYS, f"{path}: logins entry {n}")
    where = f"{path}: logins entry {n} ({fields['login']})"
    commands = fields.get("commands", [])
    if commands is None:
        commands = []
    if not isinstance(commands, list) or not all(isinstance(c, str) and c for c in commands):
        raise ToolError(f"{where}: commands has to be a list of command lines")
    via = fields.get("via")
    if via is not None and (not isinstance(via, str) or not via.strip()):
        raise ToolError(f"{where}: via has to be a command line to run the command through")
    return Login(fields["login"], list(commands), via.strip() if via else None,
                 checked(attrs.attributes, fields.get("attributes"), where),
                 entry_operations(fields.get("operations"), where), path)


def path_entry(raw: Any, n: int, path: str) -> PathInfo:
    fields = table(raw, "path", PATH_KEYS, f"{path}: paths entry {n}")
    where = f"{path}: paths entry {n} ({fields['path']})"
    for key in ("git_url", "git_branch"):
        if fields.get(key) is not None and not isinstance(fields[key], str):
            raise ToolError(f"{where}: {key} has to be a string")
    return PathInfo(fields["path"], fields.get("git_url") or None, fields.get("git_branch") or None,
                    checked(attrs.attributes, fields.get("attributes"), where),
                    entry_operations(fields.get("operations"), where),
                    checked(attrs.condition, fields.get("logins"), f"{where}: logins"), path)


def entry_operations(raw: Any, where: str) -> dict[str, str]:
    """An entry's own commands for operations: a table of name = command."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ToolError(f"{where}: operations has to be a table of name = command")
    for name, command in raw.items():
        if not isinstance(name, str) or not name:
            raise ToolError(f"{where}: operations has to be a table of name = command")
        if not isinstance(command, str) or not command.strip():
            raise ToolError(f"{where}: operations {name!r} has to be a command line")
    return {name: command.strip() for name, command in raw.items()}


OPERATION_KEYS = ("command", "operations", "logins", "paths", "clone", "serial")


def operation_entry(name: Any, raw: Any, path: str) -> Operation:
    if not isinstance(name, str) or not name:
        raise ToolError(f"{path}: operations: a name has to be a word, not {name!r}")
    where = f"{path}: operations {name!r}"
    if not isinstance(raw, dict):
        raise ToolError(f"{where}: a table with command (or operations, for a group), "
                        "logins, paths, clone, serial")
    for key in raw:
        if key not in OPERATION_KEYS:
            raise ToolError(f"{where}: unknown key {key!r}; known: {', '.join(OPERATION_KEYS)}")
    command, members = raw.get("command"), raw.get("operations")
    if (command is None) == (members is None):
        raise ToolError(f"{where}: either a command or operations (a group of them)")
    if members is not None:
        if not isinstance(members, list) or not members or not all(
                isinstance(m, str) and m for m in members):
            raise ToolError(f"{where}: operations has to be a list of operation names")
        if len(raw) > 1:
            raise ToolError(f"{where}: a group has nothing but its operations; "
                            "conditions belong to the members")
        return Operation(name, members=list(members), file=path)
    if not isinstance(command, str) or not command.strip():
        raise ToolError(f"{where}: command has to be a command line")
    for key in ("clone", "serial"):
        if not isinstance(raw.get(key, False), bool):
            raise ToolError(f"{where}: {key} has to be true or false")
    paths = raw.get("paths")
    if paths is False:
        raise ToolError(f"{where}: paths is a condition, or true for every path; "
                        "leave it out to run per login")
    if raw.get("clone") and paths is None:
        raise ToolError(f"{where}: clone goes with paths")
    return Operation(name, command.strip(), [],
                     checked(attrs.condition, raw.get("logins"), f"{where}: logins"),
                     None if paths is None else checked(attrs.condition, paths, f"{where}: paths"),
                     bool(raw.get("clone")), bool(raw.get("serial")), path)


def checked(fn, *args):
    """fn's result, its ValueError as a ToolError."""
    try:
        return fn(*args)
    except ValueError as e:
        raise ToolError(str(e)) from None


def table(raw: Any, main: str, allowed: tuple[str, ...], where: str) -> dict:
    """An entry as a dict: a bare string is the main field on its own."""
    if isinstance(raw, str):
        raw = {main: raw}
    if not isinstance(raw, dict):
        raise ToolError(f"{where}: a string or a table with {main} and {', '.join(allowed[1:])}")
    for key in raw:
        if key not in allowed:
            raise ToolError(f"{where}: unknown key {key!r}; known: {', '.join(allowed)}")
    if not isinstance(raw.get(main), str) or not raw[main]:
        raise ToolError(f"{where}: needs a {main}")
    return raw


def merge(parts: Sequence[Lists], builtin: Lists | None = None) -> Lists:
    """The parts as one, in order, first definition of anything kept with a
    warning for the others, over the builtin operations, which a part's
    replaces in place; then checked as a whole, and a name only entries
    define given a table entry of its own, without a command, so every
    operation a run can name is in the table. Raises ToolError."""
    out = Lists()
    if builtin is not None:
        out.operations.update(builtin.operations)
    seen: dict[tuple, str] = {}
    for part in parts:
        out.files.extend(part.files)
        for entry in part.logins:
            if keep(seen, ("login", entry.login, tuple(entry.commands), entry.via),
                    entry.file, f"login {entry.login}"):
                out.logins.append(entry)
        for entry in part.paths:
            if keep(seen, ("path", entry.path), entry.file, f"path {entry.path}"):
                out.paths.append(entry)
        for name, op in part.operations.items():
            if keep(seen, ("operation", name), op.file, f"operation {name}"):
                out.operations[name] = op
    for name, scope in check(out).items():
        out.operations[name] = Operation(name, paths=EVERYTHING if scope == "path" else None)
    return out


def keep(seen: dict[tuple, str], key: tuple, file: str, what: str) -> bool:
    if key in seen:
        warn(f"duplicate {what} in {file}; keeping the one in {seen[key]}")
        return False
    seen[key] = file
    return True


def check(found: Lists) -> dict[str, str]:
    """The operations as a whole: members that exist, no cycles, an entry's
    own operations agreeing with the table's on where they run. Changes
    nothing; returns the names only entries define, each with "login" or
    "path" for where it runs. Raises ToolError."""
    ops = found.operations
    for op in ops.values():
        for member in op.members:
            if member not in ops:
                raise ToolError(f"{op.file}: operations {op.name!r}: unknown member {member!r}")
    for op in ops.values():
        cycle(op, ops, [])
    entry_defined: dict[str, str] = {}  # name -> "login" or "path"
    for scope, entries in (("login", found.logins), ("path", found.paths)):
        for entry in entries:
            where = f"{entry.file}: {scope}s entry ({getattr(entry, scope)})"
            for name in entry.operations:
                op = ops.get(name)
                if op is None:
                    if entry_defined.setdefault(name, scope) != scope:
                        raise ToolError(f"{where}: operations {name!r} is a {entry_defined[name]}'s "
                                        f"operation elsewhere; a name is one or the other")
                    continue
                if op.group:
                    raise ToolError(f"{where}: operations {name!r} is a group, not a command")
                if op.per_path != (scope == "path"):
                    raise ToolError(f"{where}: operations {name!r} runs per "
                                    f"{'path' if op.per_path else 'login'}, not per {scope}")
    return entry_defined


def cycle(op: Operation, ops: dict[str, Operation], trail: list[str]) -> None:
    if op.name in trail:
        loop = trail[trail.index(op.name):] + [op.name]
        raise ToolError(f"{op.file}: operations {op.name!r}: a cycle, {' -> '.join(loop)}")
    for member in op.members:
        cycle(ops[member], ops, trail + [op.name])
