"""What the tools need from ssh itself: how to dial a probe, what ssh makes
of a destination, and whose login this is. Standard library only, so
emacsclient-auto, which runs as $EDITOR, stays quick to start."""

from __future__ import annotations

import getpass
import os
import subprocess


def login_name() -> str:
    """The login this process runs as; its uid when it has no passwd entry."""
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return str(os.getuid())


# How long a probe waits to reach a host before giving up, rather than sit
# out the TCP connect timeout, a minute or two for a host that is down.
# ssh's ConnectTimeout bounds only the connection and its handshake, not the
# authentication after it, so a password prompt still waits for its answer.
PROBE_CONNECT_TIMEOUT = 10


def probe_ssh(target: str, batch: bool = False) -> list[str]:
    """`ssh ... TARGET` for a probe, the command words still to add.

    Bounded to reach the host, as every probe is. batch never prompts: for
    the probes asked on the side, where a password prompt would come out of
    nowhere. The ones a user asks for (sshtsf -L, its folder listing), the
    socket cleanup ahead of a connection and the host-key check pssh and
    xssh run may prompt, as the connection itself does.
    """
    return (["ssh", "-o", f"ConnectTimeout={PROBE_CONNECT_TIMEOUT}"]
            + (["-o", "BatchMode=yes"] if batch else []) + [target])


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
