# curiousj-tools

Small command-line tools for working across several machines over ssh and tmux, with Emacs
as the editor on all of them. Python 3.11+, click and ruamel.yaml.

| Command | What it does |
|---|---|
| `sshtsf` | ssh to a host and `tmux new-session -A` there; remembers host, session, folder and command so a two-word invocation replaces a hand-written alias per session |
| `xssh` | one synchronized tmux pane per login plus a local shell, so one typed line runs everywhere (interactive; needs [xpanes](https://github.com/greymd/tmux-xpanes)) |
| `pssh` | the batch counterpart: one command on every login at once through GNU parallel, output tagged by login; `-P` repeats it in every listed path, `-c` cloning the ones a host lacks; `-o` runs an operation the lists file defines, `-s` one login at a time for commands that ask questions |
| `ssh-logins` | the login list behind `xssh` and `pssh`, with an fzf/menu picker |
| `pick-lines` | the multi-select picker behind `-p` and `-C`/`--pick-paths`: fzf when present, else a numbered menu |
| `emacsclient-auto` | `emacsclient` that reaches a forwarded Emacs when its socket is live, the local server otherwise; use it as `$EDITOR` on hosts you reach with `sshtsf -e` |
| `ec-browse` | opens URLs in whichever Emacs `emacsclient-auto` reaches; use it as `$BROWSER` |
| `curiousj-tools init` | prints the shell functions that go with them, for an rc file to eval: `emacs-remote` and the `tmux-env-refresh` prompt hook; see [Shell integration](#shell-integration) |

## Install

```sh
curl -LsSf https://raw.githubusercontent.com/curiousjtuber/curiousj-tools/main/install.sh | sh
```

or, with wget:

```sh
wget -qO- https://raw.githubusercontent.com/curiousjtuber/curiousj-tools/main/install.sh | sh
```

[`install.sh`](install.sh) runs `uv tool install` on this repository, first installing
[uv](https://docs.astral.sh/uv/) from astral.sh if it is missing; uv fetches a Python 3.11+
when the system has none. It needs no root, only curl or wget and git. Run it again to upgrade:
an existing install is upgraded from wherever it came from, an editable checkout included.
`CURIOUSJ_TOOLS_SOURCE` installs something else instead, such as a tag
(`git+https://github.com/curiousjtuber/curiousj-tools@v0.2.0`) or a checkout's directory.

By hand, with uv or pipx:

```sh
uv tool install git+https://github.com/curiousjtuber/curiousj-tools
```

or, from a checkout, so edits are live:

```sh
uv tool install --editable ~/src/curiousj-tools
```

`pipx install` works the same way. Either puts the commands in `~/.local/bin`.

Optional dependencies, by command:

| Tool | Needed by |
|---|---|
| `tmux` | `sshtsf`, `xssh` |
| `xpanes` | `xssh` |
| GNU `parallel` | `pssh` |
| `fzf` | pickers everywhere; without it they fall back to a numbered menu |
| `waypipe`, `socat` | `sshtsf -w` (Wayland forwarding) and `sshtsf` `ecf_port` relays |
| `emacsclient` | `emacsclient-auto`, `ec-browse`, `sshtsf -e` |

`sshtsf` treats every tool but `ssh` as optional: a missing `waypipe` (here or on the remote),
a missing `socat` on the remote or no running Emacs server is a warning on stderr, and the
connection goes ahead without that forward, `-e`/`-w` or not. A remote without `tmux` gets a
plain login shell in the session's folder, with the same warning. So does a session that
cannot be created or attached; one whose command will not start is created without it, a
shell in its first window. A socket that cannot be cleared on the remote drops the Emacs
forward, and a forward that still fails to bind is ssh's own warning, not a dead
connection. The worst case is a plain ssh.

On the remote, `sshtsf` looks in the Homebrew, Linuxbrew and `~/.local/bin` directories
before the rest of `PATH`, because a non-interactive ssh never reads the `.zprofile` that
adds them: a `brew install tmux` on a Mac needs no rc-file change. `SSHTSF_REMOTE_PATH`
(colon-separated) replaces that list.

## sshtsf

```
sshtsf                  pick a host, then a session
sshtsf devbox           pick a session on devbox
sshtsf devbox web       connect to session web on devbox
sshtsf devweb           the same, via a host+session alias
sshtsf -n devbox [NAME] register a new session, then connect
sshtsf -e devbox web    ...forwarding the local Emacs server socket (ecf)
sshtsf -w devbox web    ...forwarding Wayland, for GUI applications
sshtsf -l [HOST]        show the registered hosts and sessions
sshtsf -L [HOST]        show the live tmux sessions on the remote(s)
sshtsf -c [HOST [SESSION]]  add, edit or remove a host or session, interactively
sshtsf --edit           open the config in $VISUAL / $EDITOR
sshtsf --dry-run devbox web
```

Everything is remembered in `~/.config/sshtsf/config.toml`; see
[examples/sshtsf-config.toml](examples/sshtsf-config.toml) and `sshtsf -h` for the fields.
`sshtsf -c` walks through them: pick a host, then its settings or one of its sessions, and
each field shows its current value, blank keeps it, `-` clears it, or at the name prompt
removes the entry. A host or session named on the command line that is not registered yet
goes through the same prompts under `-c`. Connecting registers only an unknown session on a
known host, so `sshtsf devbox api` asks for `api` and then connects; an unknown host is
refused there, as the typo it usually is, with the `sshtsf -c` line that would register it.
A word typed on its own names one entry: a host's name or alias, or a session's alias. The
prompts refuse a word something else already answers to, and offer a new session its own
name as alias only while that is free. A new host is offered from the `logins` of the [ssh-lists file](#hosts-and-batch-runs) first, then
from `~/.ssh/known_hosts`. A host's `target` is the ssh destination, `[user@]host`, and is what
every connection and probe dials; there is no separate user field. A login picked from the
lists is the target as is; otherwise the user is asked for when the destination names none,
and a blank leaves it to ssh (`~/.ssh/config`, or your own name). Two users on one machine are
two host entries.

`sshtsf -w` wraps the connection in `waypipe ssh`, and a terminal such as Konsole then sees
`waypipe`, not the ssh it would otherwise title the tab after. So a waypipe session names the
tab `HOST:SESSION` itself, and the name stays on the tab after the session ends.

### Emacs routing (ecf)

`sshtsf -e HOST` reverse-forwards the local Emacs server socket to
`/tmp/emacs-remote-socket-USER` on the remote, USER being the login there, so two users on one
host each get their own. `SSHTSF_ECF_SOCKET` moves it, naming the path before the `-USER`; the
remote's `EMACSCLIENT_FORWARD_SOCKET` (below) then has to name the whole path. With
`emacsclient-auto` as the remote's `EDITOR`, `git commit`, `$EDITOR` and anything else that
calls `emacsclient` there open in the local Emacs while the forward is live, and in the
remote's own Emacs server otherwise. Files are opened through TRAMP, so the remote's rc file
has to say how the local Emacs reaches it. The remote is a poor judge of
that on its own (`hostname` knows nothing of an mDNS `.local` suffix or an ssh_config alias),
so `sshtsf -e` also sets `EMACS_REMOTE_TARGET` in the tmux session to the exact destination
it dialed. `emacs-remote`, from [Shell integration](#shell-integration), sets all of that up
from it, and is on by itself in an interactive ssh login.

`EMACS_REMOTE_TARGET` is set on every ecf attach, so a shell that was already running in the
session still holds the previous connection's name until it re-reads
`tmux show-environment`; `tmux-env-refresh` does that at every prompt.

`emacsclient-auto` passes every argument through to the real `emacsclient`, so `-h` shows that
program's help. Its own knobs are environment variables: `EMACSCLIENT_TRAMP_PREFIX` (empty means
never route remote), `EMACSCLIENT_FORWARD_SOCKET` (default `/tmp/emacs-remote-socket-USER`),
`EMACSCLIENT_BIN` (the real client, otherwise found on `PATH`) and `EMACSCLIENT_AUTO_DEBUG=1`
(say which branch was taken, on stderr). `ec-browse` takes `EC_BROWSE_EMACSCLIENT` and
`EC_BROWSE_FUNCTION` (default `browse-url`).

### Shell integration

`emacs-remote` and `tmux-env-refresh` are shell functions, since they change the shell they run in.
They ship with the package, and one line in `~/.zshrc` or `~/.bashrc` loads them, once the commands'
directory (`~/.local/bin` for `uv tool install`) is on `PATH`:

```sh
command -v curiousj-tools >/dev/null && eval "$(curiousj-tools init zsh)"   # or bash
```

| Function | What it does |
|---|---|
| `emacs-remote [ssh\|sshx] [user@host]` | points `EDITOR` at `emacsclient-auto`, `BROWSER` at `ec-browse`, and `emacsclient` at the former, with `EMACSCLIENT_TRAMP_PREFIX` naming this host as the far Emacs reaches it: the argument, else `EMACS_REMOTE_TARGET`, else `hostname -s`. `sshx` if TRAMP stalls on the prompt |
| `emacs-remote off` | back to the local Emacs only |
| `tmux-env-refresh` | a precmd / `PROMPT_COMMAND` hook: inside tmux, re-reads `WAYLAND_DISPLAY`, `DISPLAY`, `SSH_AUTH_SOCK`, `SSH_CONNECTION`, `XAUTHORITY` and `EMACS_REMOTE_TARGET` from the session at each prompt, so a shell that predates a reattach does not keep a dead connection's sockets. A new target re-runs `emacs-remote` where it is on |

Loading it also runs `emacs-remote` in an interactive shell over ssh; that is harmless without a
forward, as `emacsclient-auto` then uses the local Emacs. `TMUX_ENV_REFRESH_EXTRA` names more
variables for `tmux-env-refresh`, separated by spaces. `curiousj-tools init --path zsh` prints
where the file is, for an rc file that would rather `source` it than start python at every shell.

## Hosts and batch runs

```
ssh-logins [-N] [-a TERM]... [-p] [-f FILE]...
xssh      [-N] [-a TERM]... [-p] [-f FILE]... [-o NAME]... [-A TERM]... [--clone] [--pick-paths] [xpanes-options...]
pssh      [-n] [-s] [-N] [-a TERM]... [-p] [-f FILE]... [-i] [-P] [-A TERM]... [-c] [-C] [--] COMMAND [ARG...]
pssh      [same flags] -o NAME...
pssh      [-f FILE]... -L|--list-ops
xssh      [-f FILE]... -L|--list-ops
```

All three read the same lists, the `.toml` (or `.yaml`, `.yml`, `.json`) files in
`~/.config/ssh-lists/` (see [where they live](#where-the-lists-files-live)): the `logins` to reach,
the `paths` to work in, and the `operations` to run there. A login is a `[user@]host` string, or a
table when it needs `commands` run after login (`distrobox enter dev -nw`, `cd src`, `exec zsh`):
`xssh` runs them in that login's pane, so end them in something interactive, or the pane ends
with them. `via` is the batch counterpart, for `pssh`: a command line its command is run through
there, handed `sh -c '...'`. A path is relative to `~` unless absolute, with an optional
`git_url` and `git_branch` for `pssh -c` to clone it from. Both carry `attributes`, flags or
`key=value` pairs, for the operations and the `-a`/`-A` flags to select on.

```toml
logins = [
    { login = "alice@devbox", attributes = ["cachyos", "mise"] },
    { login = "alice@devbox", commands = ["distrobox enter dev -nw"], via = "distrobox enter dev -nw --" },
    { login = "my-mac.local", attributes = ["mac", "brew", "mise"] },
]

[[paths]]
path = "src/webapp"
git_url = "git@github.com:me/webapp.git"
git_branch = "main"
attributes = ["git", "py-project"]
```

The same in YAML, where strings and mappings mix freely:

```yaml
logins:
  - login: alice@devbox
    attributes: [cachyos, mise]
  - login: alice@devbox
    commands: [distrobox enter dev -nw]
    via: distrobox enter dev -nw --
  - login: my-mac.local
    attributes: [mac, brew, mise]
paths:
  - path: src/webapp
    git_url: git@github.com:me/webapp.git
    git_branch: main
    attributes: [git, py-project]
```

See [examples/ssh-lists.toml](examples/ssh-lists.toml) and
[examples/ssh-lists.yaml](examples/ssh-lists.yaml), and
[examples/operations.yaml](examples/operations.yaml) for operations kept in a
file of their own. `localhost` is appended to the logins for
the local side unless `-N`: entries naming the machine you are on, for your user, become
`localhost`, so one file serves every host on it. Each keeps its `commands`, `via`, attributes
and operations, so `xssh`'s local panes run the same `commands` as the other hosts' panes for
it, and `pssh` run on devbox reaches both devbox and its container. A plain `localhost` comes
first when no such entry is without a `via`.
`-a TERM` keeps the logins whose attributes satisfy the term, `mise`, `arch=x86_64`, `!mise` or
`arch!=x86_64` (quote the `!` for the shell), every term when given several; `-A` does the
same for paths. `-p` picks logins in fzf (TAB marks several) or a numbered menu; a login
listed twice, for two sets of `commands`, is shown with them so the two can be told apart.

```sh
xssh --stay                          # a synced pane per login; type once, runs everywhere
xssh -a cachyos                      # ...for the logins with that attribute only
pssh uptime                          # tagged output, all logins at once, exit = logins that failed
pssh 'cd ~/src/webapp && git status' # one word is a shell line; several words are one argv
pssh -i 'alias'                      # through `zsh -ic`, so aliases and functions exist
pssh -P git status -s                # in every listed path, on every login
pssh -C 'git pull --rebase --autostash'   # ...choosing the paths first
pssh -A git -c 'git pull --rebase --autostash' # ...the git ones, cloning any a host lacks first
pssh -n -P make                      # show the per-login script and the logins, run nothing
pssh -s 'sudo apt upgrade'           # one login at a time in the foreground, questions answered
```

A path a host does not have is skipped, unless `-c` is given and the entry has a `git_url`,
in which case it is cloned first and the command runs in the fresh clone. One where the command
fails marks that login failed.

`pssh` runs the command in a non-interactive shell: no aliases, no shell functions, no `cd`
carrying over between calls, and no login `commands` either. `xssh` gives each one a login
shell, so all of those work there. A login listed plainly and again with a `via` is run in both
places by `pssh`, and once per place: inside `distrobox enter dev -nw -- sh -c '...'` the `~` is
the container's own home, so a `git pull` there keeps the container's checkouts current
alongside the host's -- from that host too, where both entries are `localhost`. Its output is
tagged with the `via` as well, less the closing `--`, `alice@devbox[distrobox enter dev -nw]`,
so it reads apart from the host's.

Both contact a host whose key is not in `known_hosts` yet once beforehand, in the foreground, so
ssh's yes/no question is asked where it can be answered: inside a synchronized xpanes window the
answer would reach every pane, and under parallel the prompt stops the background ssh for good.

### Operations

A routine worth a name is an operation, which says itself what it runs and where, and
`pssh -o NAME` runs it. These come built in
([src/curiousj_tools/operations.toml](src/curiousj_tools/operations.toml)), and select on
attributes the lists give the logins and paths:

| | |
|---|---|
| `git-pull` | `git pull --rebase --autostash` in every path tagged `git`, cloning it where missing |
| `uv-tool-update` | `uv tool install --force --reinstall --editable .` in every path tagged `py-project` |
| `mise-update` | `mise self-update` and `mise upgrade` on the logins tagged `mise` |
| `cachy-update` | `cachy-update` on the logins tagged `cachyos`, one at a time |
| `brew-upgrade` | `brew update && brew upgrade` on the logins tagged `mac` or `brew` |
| `system-update` | a group: `cachy-update` and `brew-upgrade`, each login running the one that applies |
| `update-all` | a group: `git-pull`, `uv-tool-update`, `mise-update`, `system-update` |

A lists file adds its own, and one of the same name takes a built-in's place:

```toml
[operations.git-pull]
command = "git pull --ff-only"
paths = "git"                      # per path: in every path whose attributes match (true: all)
clone = true                       # as -c

[operations.mise-update]
command = "mise upgrade"
logins = "mise"                    # per login: on every login whose attributes match (absent: all)

[operations.apt-upgrade]
command = "sudo apt update && sudo apt upgrade"
logins = "debian"
serial = true                      # asks questions: run one login at a time, as -s

[operations.system-update]         # a group: each login runs the members that apply to it
operations = ["cachy-update", "brew-upgrade", "apt-upgrade"]
```

A condition is a term, a list of terms that all have to hold, or a table with any of `all`,
`any` and `none`. A login or path can carry its own command for an operation, and then takes
part with it whatever the condition says; the name need not be in the table at all:

```toml
[[paths]]
path = "src/legacy"
operations = { git-pull = "git pull --ff-only" }
```

Each login runs the operations that apply to it, in order, in one shell, each announced by
`== NAME` and each path by `== DIR`; a login none applies to is left alone. `-a` narrows the
logins and `-A` the paths, `-C` picks paths, `-c` clones for every per-path operation, `-n`
shows the scripts:

```sh
pssh -o update-all                   # everything, everywhere it applies
pssh -o git-pull -A py-project       # the python checkouts only
pssh -a cachyos -o system-update     # the CachyOS boxes only
pssh -n -o update-all                # the per-login scripts and who runs which
pssh -L                              # the operations, built-in first, where and what each runs
xssh -o system-update --stay         # the same, a synchronized pane per login instead
```

A command that asks questions -- a package manager, `sudo` -- cannot be answered under
parallel. `pssh -s` runs one login at a time in the foreground through `ssh -t` instead, each
announced by `== LOGIN`, and an operation with `serial = true` does that by itself. `xssh -o`
opens a synchronized pane per login the operation applies to, runs it there, then runs the
login's `commands` as a plain pane would (a shell, when it has none): one keystroke answers every
login, which suits a question every host asks alike; where
hosts ask different things, `pssh -s` is the tool. Shell functions make routines of it:

```sh
pull-all()   { pssh -o git-pull "$@"; }      # pull-all -n, pull-all -A py-project
sys-update() { pssh -o system-update "$@"; }
```

### Where the lists files live

Several files make one list, merged in this order:

| | |
|---|---|
| on the command line | `-f FILE`, repeatable: those files and no other |
| named files | `$SSH_LISTS_FILE`, colon-separated, when no `-f` |
| a search path | `$SSH_LISTS_PATH`, colon-separated directories, otherwise: every `.toml`, `.yaml`, `.yml`, `.json` file in each, by name |
| default | `~/.config/ssh-lists` (`$XDG_CONFIG_HOME/ssh-lists`), the same |

An entry defined twice -- a login with the same `commands` and `via`, a path, an operation name
-- keeps its first definition, with a warning naming both files, so a second file can add to
the first but not change it. A directory's files merge in name order, so the one that should
win sorts first.
The search path is for keeping the files in a private repo checked out on every host, and the
several files for keeping a host's own additions out of it:

```sh
export SSH_LISTS_PATH=~/src/my-lists     # holds lists.yaml, and local.yaml on one host
```

The extension names the format. YAML is read with ruamel.yaml, TOML and JSON with the standard
library.

## Development

```sh
uv run pytest
```

Tests are plain `unittest` cases (pytest runs them). `tests/test_no_internal_strings.py` keeps
the shipped files free of machine and account names; examples use `devbox`, `web`,
`~/src/webapp`.

## License

MIT
