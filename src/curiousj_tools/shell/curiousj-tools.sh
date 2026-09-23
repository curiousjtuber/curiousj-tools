# -*- Shell-script -*-
#
# The shell side of curiousj-tools, for bash and zsh alike. Load it from an rc
# file with
#
#   eval "$(curiousj-tools init zsh)"     # or bash
#
# after the directory 'uv tool install' puts the commands in (~/.local/bin) is
# on PATH. It defines:
#
#   emacs-remote [ssh|sshx] [user@host]   route emacsclient / $EDITOR / $BROWSER
#   emacs-remote off                      to the Emacs 'sshtsf -e' forwarded here,
#                                         or back to local-only
#   tmux-env-refresh                      a prompt hook: re-read the forwarded
#                                         sockets from tmux after a reattach
#
# and turns emacs-remote on for an interactive ssh login.

####
# emacs-remote
#
# From the local machine:  sshtsf -e <host>   reverse-forwards the local Emacs
#                                             server socket to the remote host,
#                                             and tells the tmux session the name
#                                             it dialed (EMACS_REMOTE_TARGET).
# On the remote:           emacs-remote       routes emacsclient / $EDITOR to the
#                                             forwarded Emacs when its socket is
#                                             live, else the local one.
# The per-call routing is emacsclient-auto's; see its docstring.

# Absolute path, resolved once: $EDITOR and $BROWSER are exec'd by tools that
# may not share this shell's PATH. Empty where emacsclient-auto is not found,
# and emacs-remote then says so instead of exporting a broken $EDITOR.
export EMACSCLIENT_AUTO=$(command -v emacsclient-auto 2>/dev/null)

# Default method is ssh; use sshx if TRAMP stalls on the remote prompt.
#
# The host in the TRAMP prefix is the one the LOCAL machine has to reach, and
# this machine is a poor judge of that: `hostname` knows nothing of the mDNS
# .local the other end dialed, nor of an ssh_config alias. So the argument
# wins, then EMACS_REMOTE_TARGET -- which `sshtsf -e` sets in the tmux session
# to the exact destination it connected to -- and `hostname -s` is only the
# guess of last resort. A target without a user gets this login's, since that
# is the account the incoming ssh landed in.
function emacs-remote {
    if [[ "$1" == off ]]; then
        unset EMACSCLIENT_TRAMP_PREFIX
        unset -f emacsclient 2>/dev/null
        export EDITOR=emacsclient
        unset BROWSER
        return
    fi
    if [[ -z $EMACSCLIENT_AUTO ]]; then
        echo "emacs-remote: emacsclient-auto not on PATH -- uv tool install git+https://github.com/curiousjtuber/curiousj-tools" >&2
        return 1
    fi
    local method="${1:-ssh}"
    local target="${2:-${EMACS_REMOTE_TARGET:-$(hostname -s)}}"
    [[ $target == *@* ]] || target="$USER@$target"
    export EMACSCLIENT_TRAMP_PREFIX="/${method}:${target}:"
    export EDITOR="$EMACSCLIENT_AUTO"
    emacsclient() { command "$EMACSCLIENT_AUTO" "$@"; }
    # Send URLs to the local machine's browser via its Emacs.
    local browse
    browse=$(command -v ec-browse 2>/dev/null) && export BROWSER="$browse"
}

####
# tmux-env-refresh: keeping a pane's environment current across reconnections
#
# tmux copies the variables named in update-environment out of the attaching
# client and into the session, on every attach and not just on create --
# WAYLAND_DISPLAY among them, which is what lets `sshtsf -w` leave waypipe's
# randomly-named per-connection socket alone. So a session reattached over a
# fresh connection is already right, and so is every window and pane opened
# after that.
#
# What is not right is a shell that was ALREADY running in a pane: it still
# holds the values it inherited whenever it started, so a GUI application it
# launches goes looking for the socket of a connection that is gone. This hook
# is the fix. tmux emits the new values as shell syntax itself, so there is
# nothing to write anywhere -- just re-read them each prompt.
#
# TMUX_ENV_REFRESH_EXTRA names more variables to refresh the same way,
# separated by spaces, for whatever else a stale value breaks in a pane.
function tmux-env-refresh {
    # Costs one string test outside tmux, which is the local machine's usual
    # case.
    [ -n "$TMUX" ] || return 0

    local out line
    local emacs_target_was="$EMACS_REMOTE_TARGET"
    # One call, not one per variable: show-environment takes at most one name,
    # so asking for five would mean five forks a prompt. Read the lot and
    # filter here.
    out=$(command tmux show-environment -s 2>/dev/null) || return 0

    while IFS= read -r line; do
        # `case` patterns and a string match rather than a loop over a list of
        # names: zsh does not word-split unquoted parameters and bash does,
        # and this file has to behave identically under both.
        #
        # Only the assignments. show-environment also emits "unset FOO;" for
        # anything the attaching client lacks, and honouring that would strip
        # SSH_AUTH_SOCK out of every pane the moment you attach from a machine
        # with no agent. The price is that a dead WAYLAND_DISPLAY lingers after
        # attaching from a non-Wayland client rather than being cleared, which
        # is much the cheaper of the two failures.
        case $line in
            WAYLAND_DISPLAY=*|DISPLAY=*|SSH_AUTH_SOCK=*|SSH_CONNECTION=*|XAUTHORITY=*)
                eval "$line" ;;
            # The name `sshtsf -e` dialed to get here, set into the session on
            # every ecf attach (see emacs-remote). Same staleness story: a
            # shell that predates this attach still carries the name some
            # earlier connection reached this host by.
            EMACS_REMOTE_TARGET=*)
                eval "$line" ;;
            *=*)
                case " $TMUX_ENV_REFRESH_EXTRA " in
                    *" ${line%%=*} "*) eval "$line" ;;
                esac ;;
        esac
    done <<< "$out"

    # A new target is only useful once it is in the TRAMP prefix, and
    # emacs-remote reads it exactly once. Re-run it -- with the method already
    # chosen, not the default -- but only where routing is on: a shell that
    # was never routed stays that way.
    if [ "$EMACS_REMOTE_TARGET" != "$emacs_target_was" ] && \
       [ -n "$EMACSCLIENT_TRAMP_PREFIX" ]; then
        local method="${EMACSCLIENT_TRAMP_PREFIX#/}"
        emacs-remote "${method%%:*}"
    fi
}

# Registered once. A guard variable rather than testing membership of
# precmd_functions, because zsh's ${array[(I)name]} would still have to PARSE
# under bash, where it does not mean anything.
if [ -z "$TMUX_ENV_REFRESH_HOOKED" ]; then
    TMUX_ENV_REFRESH_HOOKED=1
    if [ -n "$ZSH_VERSION" ]; then
        precmd_functions+=(tmux-env-refresh)
    elif [ -n "$BASH_VERSION" ]; then
        PROMPT_COMMAND="tmux-env-refresh${PROMPT_COMMAND:+; $PROMPT_COMMAND}"
    fi
fi

####
# Turn emacs-remote on for an interactive ssh login.
#
# `sshtsf -e` forwards the local machine's Emacs socket here, but routing only
# takes effect once emacs-remote is called. Doing that here saves the manual
# step after login. Safe even when the socket was not forwarded (a plain,
# non-ecf ssh): emacsclient-auto falls back to the local Emacs whenever the
# forwarded socket is not live.
if [[ -n $SSH_CONNECTION && $- == *i* && -n $EMACSCLIENT_AUTO ]]; then
    emacs-remote
fi
