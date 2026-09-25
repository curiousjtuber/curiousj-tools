"""distbuild -- set up builds shared across hosts with distcc.

    distbuild makepkg [-n|--dry-run] [--rust-cpu CPU] [--lto|--no-lto] [TARGET]
    distbuild hosts [-n|--dry-run] [-e|--extra N] [-j|--jobs N] [-l|--local] [--localslots N]
                    [-a TERM]... [-p] [-f FILE]...

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

--no-lto turns 'lto' off in OPTIONS, --lto back on; without either, OPTIONS
is left alone. With LTO the optimizing and code generation happen when
linking, on this machine, so a distcc host does only the parsing half of a
compile. A PKGBUILD's own options=(lto) or options=(!lto) still wins.

The flags are taken from /etc/makepkg.conf and /etc/makepkg.conf.d/*.conf,
the last setting of each winning as it does for makepkg. An assignment built
on them, such as CXXFLAGS="$CFLAGS ...", is copied as well: the system file
expanded it with the native CFLAGS before the user's file is read. Each is
replaced where the user's file sets it, or added at the end; the rest of the
file is kept as it is. -n prints the change as a diff instead of writing it.

'hosts' points the same file's distcc settings at the logins of the
ssh-lists files that have the 'distccd' attribute (-a replaces that term;
-p and -f as for `ssh-logins`); -l/--local adds this machine as 'localhost'.
It is left out unless asked: it already preprocesses every job and links,
and a share of the compiles on top made builds slower, not faster.
Each gets its nproc plus N (default 4) jobs in DISTCC_HOSTS. distcc takes
a first slot on every host, in list order, before a second on any, so the
limits set each host's share and the order only breaks ties: most cores
first, 'localhost' after its equals, as it preprocesses and links on top.
A login is named by its first address distccd answers on, wired before
wifi (its host name last, which mDNS may well answer with the wifi one).
A --listen in its /etc/conf.d/distccd is the only address distccd
answers on, and is reported when it is the wifi one beside a wired one.
One that cannot be reached over ssh without a prompt, or has no distccd
answering, is left out with a warning.

--localslots N (default 8) ends DISTCC_HOSTS: how many compiles run here at
once when distcc does not distribute them, among them those that failed on
a host and are retried here. distcc's own 4 would leave a build whose
compiles cannot be distributed (C++20 modules, say) at 4 wide, as a failing
host is passed over for a minute and without 'localhost' nothing is left.
N is bounded by memory more than by cores: a C++ compile can take a GB.

MAKEFLAGS and NINJAFLAGS get -j
with every job slot counted, plus this machine's nproc (-j replaces the sum):
a compile beyond the slots only waits for one, but linking, configure checks
and code generation run here and would otherwise leave a slot idle. BUILDENV gets 'distcc' turned on, from the system file
if the user's does not set it. The other lines are kept as they are.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import difflib
import os
import pathlib
import re
import shlex
import socket
import subprocess
import sys

import click

from . import attrs, cmdline, logins, pick, sshutil
from .lists import Login, ToolError

SYSTEM_CONF = pathlib.Path("/etc/makepkg.conf")
DISTCCD_CONF = "/etc/conf.d/distccd"
DISTCCD_PORT = 3632
DEFAULT_TERM = "distccd"

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
    note: str  # the comment over it when it is added at the end


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
            last[a.name] = Override(a.name, a.text, f"from {path}")
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
    there. One it lacks follows the one before it with the same note, as
    CXXFLAGS follows CFLAGS; failing that it is added at the end, under
    its note."""
    text = user_text
    appended: list[Override] = []
    after, prev = None, None  # where the previous one ended in text, and it
    for o in wanted:
        mine = [a for a in assignments(text) if a.name == o.name]
        if mine:
            a = mine[-1]
            text = text[:a.start] + o.text + text[a.end:]
            after = a.start + len(o.text)
        elif after is not None and prev.note == o.note:
            text = text[:after] + "\n" + o.text + text[after:]
            after += 1 + len(o.text)
        else:
            appended.append(o)
            after = None
        prev = o
    note = None
    for o in appended:
        if text and not text.endswith("\n"):
            text += "\n"
        if o.note != note:
            text += ("" if not text or text.endswith("\n\n") else "\n") + f"#-- {o.note}\n"
            note = o.note
        text += o.text + "\n"
    return text


@dataclasses.dataclass
class Helper:
    host: str  # as DISTCC_HOSTS names it
    nproc: int
    local: bool = False


def listen_address(distccd_conf: str) -> str | None:
    """The address DISTCC_ARGS has distccd --listen on, if it names one."""
    args = [a for a in assignments(distccd_conf) if a.name == "DISTCC_ARGS"]
    m = re.search(r"--listen[= ]+([^\s\"']+)", args[-1].text) if args else None
    return m.group(1) if m else None


def distccd_answers(host: str) -> bool:
    try:
        with socket.create_connection((host, DISTCCD_PORT), timeout=sshutil.PROBE_CONNECT_TIMEOUT):
            return True
    except OSError:
        return False


_MARK = "--distbuild--"
# Run on the login by sh, whatever its login shell: nproc, the distccd
# config, then "wired|wifi ADDRESS" for each IPv4 address of a physical
# interface.
PROBE_SCRIPT = f"""\
nproc; echo {_MARK}
cat {DISTCCD_CONF} 2>/dev/null; echo {_MARK}
for d in /sys/class/net/*; do
    [ -e "$d/device" ] || continue
    if [ -e "$d/wireless" ] || [ -e "$d/phy80211" ]; then kind=wifi; else kind=wired; fi
    ip -o -4 addr show dev "${{d##*/}}" 2>/dev/null |
        while read -r _ _ _ addr _; do echo "$kind ${{addr%/*}}"; done
done
"""


@dataclasses.dataclass
class Probed:
    """What PROBE_SCRIPT found on a login."""
    nproc: int
    listen: str | None
    wired: list[str]
    wifi: list[str]


def parse_probe(out: str) -> Probed | None:
    parts = out.split(f"{_MARK}\n")
    if len(parts) != 3 or not parts[0].strip().isdigit():
        return None
    addrs = [line.split() for line in parts[2].splitlines() if len(line.split()) == 2]
    return Probed(int(parts[0]), listen_address(parts[1]),
                  [a for kind, a in addrs if kind == "wired"],
                  [a for kind, a in addrs if kind == "wifi"])


def candidates(found: Probed, name: str) -> tuple[list[str], str | None]:
    """The addresses to try for distccd, wired first, and a warning when it
    listens on wifi only while a wired address is there. A --listen address
    is the only one distccd answers on; without one, the host name comes
    last, for an address the interfaces did not show."""
    if found.listen:
        why = None
        if found.listen in found.wifi and found.wired:
            why = (f"distccd listens on wifi {found.listen} only; --listen {found.wired[0]} "
                   f"in its {DISTCCD_CONF} would use the wired one")
        return [found.listen], why
    return found.wired + found.wifi + [name], None


def probe(login: Login) -> tuple[Helper | None, list[str]]:
    """login as a distcc helper, or None, and the warnings on the way."""
    proc = subprocess.run(
        sshutil.probe_ssh(login.login, batch=True) + ["sh", "-c", shlex.quote(PROBE_SCRIPT)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL)
    found = parse_probe(proc.stdout) if proc.returncode == 0 else None
    if found is None:
        why = proc.stderr.strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
        return None, [f"{login.login}: cannot ask for its nproc without a prompt ({why[0]}); left out"]
    name = sshutil.ssh_config(login.login).get("hostname") or login.login.rpartition("@")[2]
    tried, why = candidates(found, name)
    warnings = [f"{login.login}: {why}"] if why else []
    for host in tried:
        if distccd_answers(host):
            return Helper(host, found.nproc), warnings
    return None, warnings + [f"{login.login}: no distccd answering on "
                             f"{' or '.join(tried)}, port {DISTCCD_PORT}; left out"]


def local_nproc() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:  # macOS
        return os.cpu_count() or 1


def distcc_hosts(helpers: list[Helper], extra: int, localslots: int) -> str:
    """DISTCC_HOSTS for helpers, most cores first, 'localhost' after its equals,
    then --localslots. The same address twice (two logins on one machine)
    counts once."""
    seen, line = set(), []
    for h in sorted(helpers, key=lambda h: (-h.nproc, h.local)):
        if h.host not in seen:
            seen.add(h.host)
            line.append(f"{h.host}/{h.nproc + extra}")
    return " ".join(line + [f"--localslots={localslots}"])


def set_option(array: str, word: str, on: bool) -> str:
    """An OPTIONS=(...) or BUILDENV=(...) assignment with word on, or off
    ("!word"), added at the front when the array lacks it either way."""
    have = re.compile(rf"(?<![\w!-])!?{re.escape(word)}(?![\w-])")
    want = word if on else f"!{word}"
    if have.search(array):
        return have.sub(want, array)
    return array.replace("(", f"({want} ", 1)


def last_array(user_text: str, confs: list[tuple[pathlib.Path, str]], name: str) -> str:
    """The user's setting of the array name, else the system files' last."""
    return ([a.text for a in assignments(user_text) if a.name == name]
            or [a.text for _, text in confs for a in assignments(text) if a.name == name]
            or [f"{name}=()"])[-1]


def slots(hosts: str) -> int:
    """The jobs a DISTCC_HOSTS line allows at once on its hosts; --localslots
    and the like are not hosts."""
    return sum(int(spec.rpartition("/")[2]) for spec in hosts.split() if not spec.startswith("--"))


def host_overrides(user_text: str, confs: list[tuple[pathlib.Path, str]],
                   hosts: str, jobs: int) -> list[Override]:
    """MAKEFLAGS and NINJAFLAGS with -j jobs, BUILDENV and DISTCC_HOSTS for
    the given hosts line, in the order the system file has them."""
    note = "set by `distbuild hosts`"
    return [Override("MAKEFLAGS", f'MAKEFLAGS="-j{jobs}"', note),
            Override("NINJAFLAGS", f'NINJAFLAGS="-j{jobs}"', note),
            Override("BUILDENV", set_option(last_array(user_text, confs, "BUILDENV"), "distcc", True), note),
            Override("DISTCC_HOSTS", f'DISTCC_HOSTS="{hosts}"', note)]


def native_left(user_text: str, confs: list[tuple[pathlib.Path, str]]) -> bool:
    """Whether CFLAGS still say -march=native once the user's file is read."""
    cflags = [a.text for _, text in confs + [(None, user_text)]
              for a in assignments(text) if a.name == "CFLAGS"]
    return bool(cflags) and "-march=native" in cflags[-1]


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
@click.option("--lto/--no-lto", default=None, help="Turn 'lto' on or off in OPTIONS; default leave it.")
@click.argument("target", required=False)
def makepkg(target: str | None, rust_cpu: str | None, lto: bool | None, dry_run: bool) -> int:
    march = target or native_march()
    if not march:
        raise click.ClickException("cannot tell what -march=native is here; name the TARGET")
    rust_cpu = rust_cpu or (target if target else native_rust_cpu() or march)

    path, old, confs = read_confs()
    wanted = overrides(confs, march, rust_cpu)
    if not any(o.name == "CFLAGS" for o in wanted):
        raise click.ClickException(f"no CFLAGS in {SYSTEM_CONF}")
    what = f"{', '.join(o.name for o in wanted)} with -march={march}, target-cpu={rust_cpu}"
    if lto is not None:
        options = set_option(last_array(old, confs, "OPTIONS"), "lto", lto)
        wanted.append(Override("OPTIONS", options, "set by `distbuild makepkg`"))
        what += ", OPTIONS with " + ("lto" if lto else "!lto")
    return write(path, old, apply(old, wanted), dry_run, what)


@cli.command(cls=cmdline.Command, help=__doc__)
@click.option("-n", "--dry-run", is_flag=True, help="Print the change as a diff; write nothing.")
@click.option("-e", "--extra", metavar="N", type=click.IntRange(min=0), default=4, show_default=True,
              help="Jobs per host on top of its nproc.")
@click.option("-j", "--jobs", metavar="N", type=click.IntRange(min=1),
              help="MAKEFLAGS and NINJAFLAGS -j; default the slots plus this machine's nproc.")
@click.option("-l", "--local", is_flag=True, help="Build on this machine too, as localhost.")
@click.option("--localslots", metavar="N", type=click.IntRange(min=1), default=8, show_default=True,
              help="Compiles run here at once when not distributed.")
@logins.login_options(no_local=False)
def hosts(opts: logins.LoginOpts, extra: int, jobs: int | None, local: bool, localslots: int,
          dry_run: bool) -> int:
    path, old, confs = read_confs()
    # localhost is added here with --local, whatever its attributes: makepkg runs on it.
    remote_opts = dataclasses.replace(
        opts, no_local=True, terms=opts.terms or (attrs.term(DEFAULT_TERM, "term"),))
    try:
        # A login with a via is a place inside a machine, not one of its own.
        remote = [e for e in logins.select(remote_opts) if e.via is None]
    except ToolError as e:
        raise click.ClickException(str(e))
    except pick.Abort:
        return pick.EXIT_ABORT

    with concurrent.futures.ThreadPoolExecutor() as pool:
        probed = list(pool.map(probe, remote))
    helpers = [Helper("localhost", local_nproc(), local=True)] if local else []
    for helper, warnings in probed:
        for w in warnings:
            click.echo(f"distbuild: {w}", err=True)
        if helper:
            helpers.append(helper)
    if not helpers:
        raise click.ClickException("no hosts to build on")
    if native_left(old, confs):
        click.echo("distbuild: CFLAGS still say -march=native, which each host reads as its own "
                   "CPU; run `distbuild makepkg` too", err=True)

    line = distcc_hosts(helpers, extra, localslots)
    jobs = jobs or slots(line) + local_nproc()
    return write(path, old, apply(old, host_overrides(old, confs, line, jobs)), dry_run,
                 f'DISTCC_HOSTS="{line}", -j{jobs}')


def read_confs() -> tuple[pathlib.Path, str, list[tuple[pathlib.Path, str]]]:
    """The user's makepkg.conf, its text ("" while missing) and the system files'."""
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
    return path, (path.read_text() if path.exists() else ""), confs


def write(path: pathlib.Path, old: str, new: str, dry_run: bool, what: str) -> int:
    """new to path, or with dry_run the change as a diff; what says what it set."""
    if dry_run:
        sys.stdout.writelines(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            str(path) if old else "/dev/null", str(path)))
        return 0
    if new != old:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new)
    click.echo(f"{path}: {what}" + ("" if new != old else " (unchanged)"))
    return 0


def main(argv: list[str] | None = None) -> int:
    return cmdline.run(cli, argv, "distbuild")


if __name__ == "__main__":
    sys.exit(main())
