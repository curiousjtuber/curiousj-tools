"""ssh-hosts -- print the host list xssh and pssh work from, optionally picking a subset.

    ssh-hosts [-h] [-N|--no-local] [-p|--pick] [-f HOSTFILE]

One entry per line: every [user@]host in the host file, then `localhost`
last, which consumers turn into whatever "this machine" means for them
(xssh: a local pane, pssh: a local run); -N/--no-local leaves it out.
Entries that name this machine are dropped, so one list can serve every
host on it. -p/--pick shows the list in fzf (TAB marks several) and prints
only the marked entries; without fzf, a numbered menu (see `pick-lines -h`).
Aborting the picker exits 130 with nothing printed.

The host file is one [user@]host per line; blank lines and #-comments are
ignored. Without -f the first readable of these is used:

    $SSH_HOSTS                      explicit override
    $SSH_LISTS_DIR/ssh-hosts   a directory of lists, e.g. a private repo
    ~/.config/ssh-hosts             ($XDG_CONFIG_HOME/ssh-hosts)
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass

from . import pick

EXIT_ERROR = 1
EXIT_USAGE = 2


class HostsError(Exception):
    """A user-facing failure: the message is printed and the tool exits 1."""


class UsageError(HostsError):
    """A bad command line: the message is printed and the tool exits 2."""


@dataclass
class HostOpts:
    """The host-selection flags xssh, pssh and pull-all share with ssh-hosts."""

    no_local: bool = False
    pick: bool = False
    file: str | None = None

    def argv(self) -> list[str]:
        out = []
        if self.no_local:
            out.append("-N")
        if self.pick:
            out.append("-p")
        if self.file:
            out += ["-f", self.file]
        return out


def take_host_opt(argv: list[str], i: int, opts: HostOpts, prog: str) -> int:
    """Consume argv[i] into opts if it is a host flag; return the new index, or i."""
    arg = argv[i]
    if arg in ("-N", "--no-local"):
        opts.no_local = True
        return i + 1
    if arg in ("-p", "--pick"):
        opts.pick = True
        return i + 1
    if arg == "-f":
        if i + 1 >= len(argv):
            raise UsageError("-f needs a file")
        opts.file = argv[i + 1]
        return i + 2
    return i


LISTS_DIR_VAR = "SSH_LISTS_DIR"


def find_config(explicit: str | None, var: str, name: str, env=None) -> str:
    """The first readable of: explicit, $var, $SSH_LISTS_DIR/name,
    $XDG_CONFIG_HOME/name. The directory variable is the one to set when
    the lists are kept together, say in a private repo checked out on
    every host."""
    env = os.environ if env is None else env
    if explicit:
        if not os.access(explicit, os.R_OK):
            raise HostsError(f"cannot read {explicit}")
        return explicit
    home = env.get("HOME") or os.path.expanduser("~")
    config_home = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    lists_dir = env.get(LISTS_DIR_VAR)
    candidates = [env.get(var),
                  os.path.join(lists_dir, name) if lists_dir else None,
                  os.path.join(config_home, name)]
    for path in candidates:
        if path and os.access(path, os.R_OK):
            return path
    raise HostsError(f"no {name} list: set {var} or {LISTS_DIR_VAR}, or create ~/.config/{name}")


def read_list(path: str) -> list[str]:
    """Non-blank, non-comment lines, stripped."""
    with open(path) as f:
        lines = [line.strip() for line in f]
    return [line for line in lines if line and not line.startswith("#")]


def self_names() -> set[str]:
    """Lowercased names this machine answers to: hostname, its first label,
    the loopback names and, on macOS, the Bonjour name (`my-mac.local` for a
    host called MyMac)."""
    names = {"localhost", "127.0.0.1", "::1"}
    for name in (socket.gethostname(), os.uname().nodename):
        if name:
            names.add(name.lower())
            names.add(name.lower().split(".", 1)[0])
    if shutil.which("scutil"):
        proc = subprocess.run(["scutil", "--get", "LocalHostName"],
                              capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            names.add(proc.stdout.strip().lower())
    return names


def drop_self(entries: list[str], names: set[str], user: str) -> list[str]:
    """Entries minus those naming this machine: the list is shared between
    hosts, so each one's own name is in it, and `localhost` already stands
    for it. An entry for another user on this machine is kept: that is a
    different login, not this one."""
    kept = []
    for entry in entries:
        entry_user, _, host = entry.rpartition("@")
        host = host.lower()
        mine = host in names or host.split(".", 1)[0] in names
        if mine and (not entry_user or entry_user == user):
            continue
        kept.append(entry)
    return kept


def ssh_config(entry: str) -> dict[str, str]:
    """What `ssh -G` resolves for entry after ~/.ssh/config: keyword -> value,
    keywords lowercased. Empty if ssh is missing or refuses the name."""
    proc = subprocess.run(["ssh", "-G", entry], capture_output=True, text=True)
    if proc.returncode != 0:
        return {}
    out = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition(" ")
        out.setdefault(key.lower(), value)
    return out


def host_known(entry: str) -> bool:
    """Whether the key of the host entry resolves to is in a known_hosts file
    ssh would consult (user and global ones, hashed entries included)."""
    cfg = ssh_config(entry)
    host = cfg.get("hostname") or entry.rpartition("@")[2]
    port = cfg.get("port", "22")
    name = host if port == "22" else f"[{host}]:{port}"
    files = (cfg.get("userknownhostsfile", "~/.ssh/known_hosts") + " "
             + cfg.get("globalknownhostsfile", "")).split()
    for f in files:
        f = os.path.expanduser(f)
        if not os.path.exists(f):
            continue
        if subprocess.run(["ssh-keygen", "-F", name, "-f", f],
                          capture_output=True).returncode == 0:
            return True
    return False


def confirm_new_hosts(entries: list[str], prog: str) -> None:
    """ssh once, in the foreground, to each host whose key is not known yet,
    so the yes/no question is asked here. Asked from inside a synchronized
    xpanes window the answer lands in every pane, and under parallel the
    prompt cannot be answered at all: ssh runs in a background process
    group there, and reading /dev/tty stops it. Raises HostsError when a
    host is refused or unreachable."""
    for entry in entries:
        if entry == "localhost" or host_known(entry):
            continue
        print(f"{prog}: {entry}: host key not known yet, connecting once to confirm it",
              file=sys.stderr)
        if subprocess.run(["ssh", entry, "true"]).returncode != 0:
            raise HostsError(f"{entry}: host key not confirmed (answer yes, or drop it from the list)")


def hosts(opts: HostOpts, env=None) -> list[str]:
    """The resolved, filtered, optionally picked list. Raises HostsError or
    pick.Abort."""
    path = find_config(opts.file, "SSH_HOSTS", "ssh-hosts", env)
    entries = drop_self(read_list(path), self_names(), getpass.getuser())
    if not entries and opts.no_local:
        raise HostsError(f"no hosts in {path} (entries naming this machine are dropped)")
    if not opts.no_local:
        entries.append("localhost")
    if opts.pick:
        entries = pick.pick(entries, "hosts")
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ssh-hosts", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-N", "--no-local", action="store_true", help="leave localhost out")
    parser.add_argument("-p", "--pick", action="store_true", help="choose entries interactively")
    parser.add_argument("-f", metavar="HOSTFILE", dest="file", help="host list to use")
    args = parser.parse_args(argv)
    try:
        entries = hosts(HostOpts(args.no_local, args.pick, args.file))
    except HostsError as e:
        print(f"ssh-hosts: {e}", file=sys.stderr)
        return EXIT_ERROR
    except pick.Abort:
        return pick.EXIT_ABORT
    print("\n".join(entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
