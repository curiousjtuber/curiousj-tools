"""The one list file behind ssh-hosts, xssh and pssh: hosts to reach, paths to work in.

TOML, YAML or JSON, told apart by the extension: ssh-lists.toml, .yaml, .yml
or .json. Two lists, both optional:

    hosts = [
        "alice@devbox",                     # [user@]host, as ssh takes it
        { host = "build.example.com", commands = ["distrobox enter dev"] },
    ]                                       # commands: run after login, in the pane xssh opens

    [[paths]]
    path = "src/webapp"                     # relative to ~ unless absolute
    git_url = "git@github.com:me/webapp.git"    # optional: what `pssh -c` clones it from
    git_branch = "main"                     # optional: -b for that clone

A host is a string or a table (`[[hosts]]` tables work too, when none is
a bare string); so is a path. In YAML the same reads:

    hosts:
      - alice@devbox
      - host: build.example.com
        commands: [distrobox enter dev]
    paths:
      - path: src/webapp
        git_url: git@github.com:me/webapp.git

The file is found by, first readable wins:

    -f FILE                     on the command line
    $SSH_LISTS_FILE             one file, wherever it is
    $SSH_LISTS_PATH             colon-separated directories, each tried for
                                ssh-lists.toml, .yaml, .yml, .json in that order;
                                default $XDG_CONFIG_HOME (~/.config)
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from typing import Any

NAME = "ssh-lists"
SUFFIXES = (".toml", ".yaml", ".yml", ".json")
FILE_VAR = "SSH_LISTS_FILE"
PATH_VAR = "SSH_LISTS_PATH"


class HostsError(Exception):
    """A user-facing failure: the message is printed and the tool exits 1."""


@dataclass
class HostInfo:
    """One host: the ssh destination and what to run once logged in."""

    host: str
    commands: list[str] = field(default_factory=list)


@dataclass
class PathInfo:
    """One directory pssh works in, and where a missing one is cloned from."""

    path: str
    git_url: str | None = None
    git_branch: str | None = None


@dataclass
class Lists:
    """The file: its two lists and where it was read from."""

    hosts: list[HostInfo] = field(default_factory=list)
    paths: list[PathInfo] = field(default_factory=list)
    file: str = ""


def search_path(env: Any = None) -> list[str]:
    """The directories tried for ssh-lists.*: $SSH_LISTS_PATH, else the XDG
    config home."""
    env = os.environ if env is None else env
    raw = env.get(PATH_VAR)
    if raw:
        return [os.path.expanduser(d) for d in raw.split(":") if d]
    home = env.get("HOME") or os.path.expanduser("~")
    return [env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")]


def find_file(explicit: str | None, env: Any = None) -> str:
    """The lists file to read; see the module docstring for the order.
    Raises HostsError when a named file is unreadable or none is found."""
    env = os.environ if env is None else env
    named = explicit or env.get(FILE_VAR)
    if named:
        if not os.access(named, os.R_OK):
            raise HostsError(f"cannot read {named}" + ("" if explicit else f" (${FILE_VAR})"))
        return named
    dirs = search_path(env)
    for d in dirs:
        for suffix in SUFFIXES:
            path = os.path.join(d, NAME + suffix)
            if os.access(path, os.R_OK):
                return path
    raise HostsError(
        f"no {NAME}{'/'.join(SUFFIXES)} in {':'.join(dirs)}: set ${FILE_VAR} or ${PATH_VAR}, "
        f"or create ~/.config/{NAME}.toml (or .yaml, .yml, .json)")


def read_data(path: str) -> Any:
    """The file decoded by its extension. Raises HostsError on a format the
    name does not tell, or a file that does not parse."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in SUFFIXES:
        raise HostsError(f"{path}: the extension has to say the format: one of "
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
        raise HostsError(f"{path}: {e}") from None


def read_yaml(path: str) -> Any:
    # Imported here so the TOML and JSON readers, and everything that only
    # lists hosts, start without loading a YAML library.
    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError
    try:
        with open(path) as f:
            return YAML(typ="safe").load(f)
    except YAMLError as e:
        raise HostsError(f"{path}: {str(e).splitlines()[0]}") from None


def load(path: str) -> Lists:
    """The file, checked: every entry the shape the docstring shows. Raises
    HostsError naming the entry otherwise."""
    return parse(read_data(path), path)


def parse(data: Any, path: str = NAME) -> Lists:
    if data is None:  # an empty YAML document
        data = {}
    if not isinstance(data, dict):
        raise HostsError(f"{path}: the file has to be a table (mapping) with hosts and paths")
    for key in data:
        if key not in ("hosts", "paths"):
            raise HostsError(f"{path}: unknown key {key!r}; the lists are hosts and paths")
    hosts = [host_entry(e, n, path) for n, e in enumerate(entries(data, "hosts", path), 1)]
    paths = [path_entry(e, n, path) for n, e in enumerate(entries(data, "paths", path), 1)]
    return Lists(hosts, paths, path)


def entries(data: dict, key: str, path: str) -> list:
    found = data.get(key, [])
    if found is None:
        return []
    if not isinstance(found, list):
        raise HostsError(f"{path}: {key} has to be a list")
    return found


def host_entry(raw: Any, n: int, path: str) -> HostInfo:
    fields = table(raw, "host", ("host", "commands"), f"{path}: hosts entry {n}")
    commands = fields.get("commands", [])
    if commands is None:
        commands = []
    if not isinstance(commands, list) or not all(isinstance(c, str) and c for c in commands):
        raise HostsError(f"{path}: hosts entry {n} ({fields['host']}): "
                         "commands has to be a list of command lines")
    return HostInfo(fields["host"], list(commands))


def path_entry(raw: Any, n: int, path: str) -> PathInfo:
    fields = table(raw, "path", ("path", "git_url", "git_branch"), f"{path}: paths entry {n}")
    for key in ("git_url", "git_branch"):
        if fields.get(key) is not None and not isinstance(fields[key], str):
            raise HostsError(f"{path}: paths entry {n} ({fields['path']}): {key} has to be a string")
    return PathInfo(fields["path"], fields.get("git_url") or None, fields.get("git_branch") or None)


def table(raw: Any, main: str, allowed: tuple[str, ...], where: str) -> dict:
    """An entry as a dict: a bare string is the main field on its own."""
    if isinstance(raw, str):
        raw = {main: raw}
    if not isinstance(raw, dict):
        raise HostsError(f"{where}: a string or a table with {main} and {', '.join(allowed[1:])}")
    for key in raw:
        if key not in allowed:
            raise HostsError(f"{where}: unknown key {key!r}; known: {', '.join(allowed)}")
    if not isinstance(raw.get(main), str) or not raw[main]:
        raise HostsError(f"{where}: needs a {main}")
    return raw
