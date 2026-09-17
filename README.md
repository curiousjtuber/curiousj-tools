# curiousj-tools

Small command-line tools for working across several machines over ssh and tmux, with Emacs
as the editor on all of them. Python 3.11+, standard library only.

| Command | What it does |
|---|---|
| `sshtsf` | ssh to a host and `tmux new-session -A` there; remembers host, session, folder and command so a two-word invocation replaces a hand-written alias per session |
| `xssh` | one synchronized tmux pane per host plus a local shell, so one typed line runs everywhere (interactive; needs [xpanes](https://github.com/greymd/tmux-xpanes)) |
| `pssh` | the batch counterpart: one command on every host at once through GNU parallel, output tagged by host; `-d` repeats it in every listed directory, `-c` cloning the ones a host lacks |
| `ssh-hosts` | the host list behind `xssh` and `pssh`, with an fzf/menu picker |
| `pick-lines` | the multi-select picker the others use: fzf when present, else a numbered menu |
| `emacsclient-auto` | `emacsclient` that reaches a forwarded Emacs when its socket is live, the local server otherwise; use it as `$EDITOR` on hosts you reach with `sshtsf -e` |
| `ec-browse` | opens URLs in whichever Emacs `emacsclient-auto` reaches; use it as `$BROWSER` |

## Install

```sh
uv tool install git+https://github.com/curiousjtuber/curiousj-tools
# or, from a checkout, so edits are live:
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
cannot be created or attached, or whose command will not start; a socket that cannot be
cleared on the remote drops the Emacs forward, and a forward that still fails to bind is
ssh's own warning, not a dead connection. The worst case is a plain ssh.

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
sshtsf devbox -n [NAME] register a new session, then connect
sshtsf -e devbox web    ...forwarding the local Emacs server socket (ecf)
sshtsf -w devbox web    ...forwarding Wayland, for GUI applications
sshtsf remote devbox    git remote add, for the current repo's twin on devbox
sshtsf set devbox ecf true
sshtsf edit             open the config in $VISUAL / $EDITOR
sshtsf --dry-run devbox web
```

Everything is remembered in `~/.config/sshtsf/config.toml`; see
[examples/sshtsf-config.toml](examples/sshtsf-config.toml) and `sshtsf -h` for the fields.
A host's `target` is the ssh destination, `[user@]host`, and is what every connection, probe
and `sshtsf remote` URL dials; there is no separate user field. `sshtsf add` asks for the user
when the destination names none, and a blank leaves it to ssh (`~/.ssh/config`, or your own
name). Two users on one machine are two host entries.

### Emacs routing (ecf)

`sshtsf -e HOST` reverse-forwards the local Emacs server socket to `/tmp/emacs-remote-socket`
on the remote. With `emacsclient-auto` as the remote's `EDITOR`, `git commit`, `$EDITOR` and
anything else that calls `emacsclient` there open in the local Emacs while the forward is
live, and in the remote's own Emacs server otherwise. Files are opened through TRAMP, so the
remote's rc file has to say how the local Emacs reaches it. The remote is a poor judge of
that on its own (`hostname` knows nothing of an mDNS `.local` suffix or an ssh_config alias),
so `sshtsf -e` also sets `EMACS_REMOTE_TARGET` in the tmux session to the exact destination
it dialed, and the rc file should prefer it:

```sh
# On the remote host, in .zshrc / .bashrc:
if command -v emacsclient-auto >/dev/null; then
    export EDITOR=emacsclient-auto
    target="${EMACS_REMOTE_TARGET:-$(hostname -s)}"
    [[ $target == *@* ]] || target="$USER@$target"
    export EMACSCLIENT_TRAMP_PREFIX="/ssh:$target:"
    export BROWSER=ec-browse
fi
```

`EMACS_REMOTE_TARGET` is set on every ecf attach, so a shell that was already running in the
session still holds the previous connection's name until it re-reads
`tmux show-environment`; a precmd hook that does so keeps long-lived panes current.

`emacsclient-auto` passes every argument through to the real `emacsclient`, so `-h` shows that
program's help. Its own knobs are environment variables: `EMACSCLIENT_TRAMP_PREFIX` (empty means
never route remote), `EMACSCLIENT_FORWARD_SOCKET` (default `/tmp/emacs-remote-socket`),
`EMACSCLIENT_BIN` (the real client, otherwise found on `PATH`) and `EMACSCLIENT_AUTO_DEBUG=1`
(say which branch was taken, on stderr). `ec-browse` takes `EC_BROWSE_EMACSCLIENT` and
`EC_BROWSE_FUNCTION` (default `browse-url`).

## Hosts and batch runs

```
ssh-hosts [-N] [-p] [-f HOSTFILE]
xssh      [-N] [-p] [-f HOSTFILE] [xpanes-options...]
pssh      [-n] [-N] [-p] [-f HOSTFILE] [-i] [-d] [-c] [-r DIRFILE] [-D] [--] COMMAND [ARG...]
```

The host list is `~/.config/ssh-hosts` (or `-f`, see below): one `[user@]host` per line,
blank lines and `#` comments ignored. `localhost` is appended for the local side unless
`-N`, and entries naming the machine you are on are dropped, so one list serves every host on
it. `-p` picks hosts in fzf (TAB marks several) or a numbered menu.

```sh
xssh --stay                          # a synced pane per host; type once, runs everywhere
pssh uptime                          # tagged output, all hosts at once, exit = hosts that failed
pssh 'cd ~/src/webapp && git status' # one word is a shell line; several words are one argv
pssh -i 'alias'                      # through `zsh -ic`, so aliases and functions exist
pssh -d git status -s                # in every listed directory, on every host
pssh -D 'git pull --rebase --autostash'   # ...choosing the directories first
pssh -d -c 'git pull --rebase --autostash' # ...cloning any checkout a host lacks first
pssh -n -d make                      # show the per-host script and the hosts, run nothing
```

`-d` reads `~/.config/ssh-dirs.toml` (or `-r`), a list of `[[dir]]` tables:

```toml
[[dir]]
path = "src/webapp"                     # relative to ~ unless absolute
url = "git@github.com:me/webapp.git"    # optional: where `-c` clones it from
branch = "main"                         # optional: -b for that clone
```

A directory a host does not have is skipped, unless `-c` is given and the entry has a `url`,
in which case it is cloned first and the command runs in the fresh clone. One where the command
fails marks that host failed. See [examples/ssh-dirs.toml](examples/ssh-dirs.toml). A shell
alias makes a routine of it, and gives a new machine its checkouts on the first run:

```sh
alias pull-all="pssh -d -c 'git pull --rebase --autostash'"
```

`pssh` runs the command in a non-interactive shell: no aliases, no shell functions, no `cd`
carrying over between calls. `xssh` gives each host a login shell, so all of those work there.

Both contact a host whose key is not in `known_hosts` yet once beforehand, in the foreground, so
ssh's yes/no question is asked where it can be answered: inside a synchronized xpanes window the
answer would reach every pane, and under parallel the prompt stops the background ssh for good.

### Where the lists live

Both lists are looked up the same way, first readable wins:

| | hosts | directories |
|---|---|---|
| on the command line | `-f FILE` | `-r FILE` |
| one file | `$SSH_HOSTS` | `$SSH_DIRS` |
| one directory holding both | `$SSH_LISTS_DIR/ssh-hosts` | `$SSH_LISTS_DIR/ssh-dirs.toml` |
| default | `~/.config/ssh-hosts` | `~/.config/ssh-dirs.toml` |

The directory form is for keeping the lists in a private repo checked out on every host:

```sh
export SSH_LISTS_DIR=~/src/my-lists     # contains ssh-hosts and ssh-dirs.toml
```

## Development

```sh
uv run pytest
```

Tests are plain `unittest` cases (pytest runs them). `tests/test_no_internal_strings.py` keeps
the shipped files free of machine and account names; examples use `devbox`, `web`,
`~/src/webapp`.

## License

MIT
