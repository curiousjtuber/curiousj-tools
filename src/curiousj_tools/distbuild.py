"""distbuild -- set up builds shared across hosts with distcc.

    distbuild makepkg [-n|--dry-run] [--rust-cpu CPU] [TARGET]

'makepkg' writes the compiler flags of the system makepkg.conf into the
user's, ~/.config/pacman/makepkg.conf (under XDG_CONFIG_HOME when set), with
the CPU named: "-march=native" becomes "-march=TARGET" in CFLAGS, and
"target-cpu=native" becomes "target-cpu=CPU" in RUSTFLAGS. A distcc host
compiles for its own CPU when told "native", so a package built across
several would be a mix; a named CPU is the same everywhere.

TARGET defaults to what `gcc -march=native` resolves to here, and CPU to
TARGET, or to what rustc resolves "native" to when TARGET is not given.
Whatever -march the system file has is replaced, and an "-mtune=native"
becomes "-mtune=TARGET" alongside it.

The flags are taken from /etc/makepkg.conf and /etc/makepkg.conf.d/*.conf,
the last setting of each winning as it does for makepkg. An assignment built
on them, such as CXXFLAGS="$CFLAGS ...", is copied as well: the system file
expanded it with the native CFLAGS before the user's file is read. Each is
replaced where the user's file sets it, or added at the end; the rest of the
file is kept as it is. -n prints the change as a diff instead of writing it.
"""

from __future__ import annotations

import dataclasses
import difflib
import os
import pathlib
import re
import subprocess
import sys

import click

from . import cmdline

SYSTEM_CONF = pathlib.Path("/etc/makepkg.conf")

_NAME = re.compile(r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)=", re.M)
# A flag value stops at a closing quote or a line continuation, not only at a space.
_VALUE = r"""[^\s"'\\]+"""


def user_conf() -> pathlib.Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return pathlib.Path(config_home) / "pacman" / "makepkg.conf"


def system_confs(conf: pathlib.Path = SYSTEM_CONF) -> list[pathlib.Path]:
    """The files makepkg reads before the user's, in its order."""
    return [conf] + sorted(pathlib.Path(f"{conf}.d").glob("*.conf"))


@dataclasses.dataclass
class Assignment:
    name: str
    start: int
    end: int
    text: str


def _line_end(text: str, i: int) -> int:
    """Where the logical line through i ends: at the first newline outside
    quotes, parentheses and backslash continuations. A comment ends it too."""
    quote, depth = None, 0
    while i < len(text):
        c = text[i]
        if quote == "'":
            if c == "'":
                quote = None
        elif c == "\\":
            i += 1
        elif quote == '"':
            if c == '"':
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "#" and (i == 0 or text[i - 1] in " \t\n"):
            nl = text.find("\n", i)
            i = len(text) if nl < 0 else nl
            continue
        elif c == "\n" and depth <= 0:
            return i
        i += 1
    return len(text)


def assignments(text: str) -> list[Assignment]:
    """The shell variable assignments in text, one per logical line, in order.
    A line inside a multi-line value is part of that value, not one of its own."""
    found, pos = [], 0
    while (m := _NAME.search(text, pos)):
        end = _line_end(text, m.end())
        found.append(Assignment(m.group(1), m.start(), end, text[m.start():end]))
        pos = end
    return found


def _references(text: str, name: str) -> bool:
    value = text.split("=", 1)[1]
    return re.search(rf"\$\{{?{name}\b", value) is not None


@dataclasses.dataclass
class Override:
    name: str
    text: str
    source: pathlib.Path


def overrides(confs: list[tuple[pathlib.Path, str]], march: str, rust_cpu: str) -> list[Override]:
    """The assignments to put in the user's file, from the system files' text:
    the last CFLAGS and RUSTFLAGS with the CPU named, and each assignment
    built on either of them. One the system files do not set is left out."""
    subs = [(re.compile(rf"-march={_VALUE}"), f"-march={march}"),
            (re.compile(r"-mtune=native\b"), f"-mtune={march}"),
            (re.compile(rf"target-cpu={_VALUE}"), f"target-cpu={rust_cpu}")]
    last: dict[str, Override] = {}
    for path, text in confs:
        for a in assignments(text):
            last.pop(a.name, None)  # re-inserted, so the order is the last setting's
            last[a.name] = Override(a.name, a.text, path)
    picked = []
    for key in ("CFLAGS", "RUSTFLAGS"):
        if key in last:
            picked.append(last[key])
            picked += [o for o in last.values() if o.name != key and _references(o.text, key)]
    for o in picked:
        for pattern, replacement in subs:
            o.text = pattern.sub(replacement, o.text)
    return picked


def apply(user_text: str, wanted: list[Override]) -> str:
    """user_text with each wanted assignment in place of its last setting
    there. One it lacks follows the one before it from the same system file,
    as CXXFLAGS follows CFLAGS; failing that it is added at the end, under
    the file it came from."""
    text = user_text
    appended: list[Override] = []
    after, prev = None, None  # where the previous one ended in text, and it
    for o in wanted:
        mine = [a for a in assignments(text) if a.name == o.name]
        if mine:
            a = mine[-1]
            text = text[:a.start] + o.text + text[a.end:]
            after = a.start + len(o.text)
        elif after is not None and prev.source == o.source:
            text = text[:after] + "\n" + o.text + text[after:]
            after += 1 + len(o.text)
        else:
            appended.append(o)
            after = None
        prev = o
    source = None
    for o in appended:
        if text and not text.endswith("\n"):
            text += "\n"
        if o.source != source:
            text += ("" if not text or text.endswith("\n\n") else "\n") + f"#-- from {o.source}\n"
            source = o.source
        text += o.text + "\n"
    return text


def native_march() -> str | None:
    """What `gcc -march=native` means on this machine, if gcc says."""
    try:
        out = subprocess.run(["gcc", "-march=native", "-Q", "--help=target"],
                             capture_output=True, text=True).stdout
    except OSError:
        return None
    m = re.search(r"^\s*-march=\s+(\S+)", out, re.M)
    return m.group(1) if m else None


def native_rust_cpu() -> str | None:
    """What rustc's target-cpu=native means on this machine, if rustc says."""
    try:
        out = subprocess.run(["rustc", "--print", "target-cpus"],
                             capture_output=True, text=True).stdout
    except OSError:
        return None
    m = re.search(r"^\s*native\b.*\(currently (\S+?)\)", out, re.M)
    return m.group(1) if m else None


@click.group(cls=cmdline.Group, help=__doc__)
def cli() -> None:
    pass


@cli.command(cls=cmdline.Command, help=__doc__)
@click.option("-n", "--dry-run", is_flag=True, help="Print the change as a diff; write nothing.")
@click.option("--rust-cpu", metavar="CPU", help="The target-cpu for RUSTFLAGS; default TARGET.")
@click.argument("target", required=False)
def makepkg(target: str | None, rust_cpu: str | None, dry_run: bool) -> int:
    march = target or native_march()
    if not march:
        raise click.ClickException("cannot tell what -march=native is here; name the TARGET")
    rust_cpu = rust_cpu or (target if target else native_rust_cpu() or march)

    path = user_conf()
    legacy = pathlib.Path(os.path.expanduser("~/.makepkg.conf"))
    if not path.exists() and legacy.exists():
        # makepkg reads the legacy file only while the XDG one is missing.
        raise click.ClickException(f"makepkg reads {legacy}; move it to {path} first")

    confs = []
    for conf in system_confs(SYSTEM_CONF):
        try:
            confs.append((conf, conf.read_text()))
        except OSError as e:
            raise click.ClickException(f"cannot read {conf}: {e.strerror}")
    wanted = overrides(confs, march, rust_cpu)
    if not any(o.name == "CFLAGS" for o in wanted):
        raise click.ClickException(f"no CFLAGS in {SYSTEM_CONF}")

    old = path.read_text() if path.exists() else ""
    new = apply(old, wanted)
    if dry_run:
        sys.stdout.writelines(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            str(path) if old else "/dev/null", str(path)))
        return 0
    if new != old:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new)
    names = ", ".join(o.name for o in wanted)
    click.echo(f"{path}: {names} with -march={march}, target-cpu={rust_cpu}"
               + ("" if new != old else " (unchanged)"))
    return 0


def main(argv: list[str] | None = None) -> int:
    return cmdline.run(cli, argv, "distbuild")


if __name__ == "__main__":
    sys.exit(main())
