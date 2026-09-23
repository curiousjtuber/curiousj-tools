"""pick-lines -- let the user mark some of the given items; print the marked ones.

    pick-lines [-h] NOUN ITEM...

The picker behind `ssh-logins -p` and `pssh -C`: fzf when it is installed
and there is a terminal to draw on (TAB marks several, ENTER confirms), else
a numbered menu on the tty. NOUN names the items in the prompts ("logins",
"paths"). Prints the marked items one per line; aborting (ESC, q, an empty
answer) exits 130 with nothing printed.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable

EXIT_ABORT = 130


class Abort(Exception):
    """The user left the picker without choosing anything."""


def pick_from(items: list, noun: str, label: Callable) -> list:
    """pick() over items shown as label(item), the chosen items back. Two
    items with one label are told apart by a number, so choosing one does
    not choose both."""
    seen: dict[str, int] = {}
    labels = []
    for item in items:
        text = label(item)
        seen[text] = seen.get(text, 0) + 1
        labels.append(text if seen[text] == 1 else f"{text} #{seen[text]}")
    chosen = set(pick(labels, noun))
    return [item for item, text in zip(items, labels) if text in chosen]


def fzf_usable() -> bool:
    """Whether fzf is installed and has a terminal to draw on."""
    return bool(shutil.which("fzf")) and sys.stdin.isatty()


def fzf_argv(prompt: str, header: str = "") -> list[str]:
    """fzf as every picker here runs it, under the cursor rather than full screen."""
    return (["fzf", "--prompt", prompt, "--height", "60%", "--reverse"]
            + (["--header", header] if header else []))


def print_menu(items: list[str], out) -> None:
    for i, item in enumerate(items, 1):
        print(f"  {i:2d}) {item}", file=out)


def fzf_pick(items: list[str], noun: str) -> list[str]:
    """Run fzf over items; its UI goes to the tty, the marked lines come back."""
    proc = subprocess.run(
        fzf_argv(f"{noun}> ", f"TAB marks {noun}, ENTER confirms") + ["--multi"],
        input="".join(f"{i}\n" for i in items), stdout=subprocess.PIPE, text=True,
    )
    sel = proc.stdout.splitlines()
    if proc.returncode != 0 or not sel:
        raise Abort
    return sel


def parse_menu_reply(reply: str, count: int) -> list[int] | None:
    """1-based indexes a menu answer names, [] for "all", None for an abort.

    Raises ValueError naming the first token that is not a choice.
    """
    reply = reply.replace(",", " ").strip()
    if reply in ("", "q", "Q"):
        return None
    if reply in ("a", "A"):
        return []
    picked = []
    for tok in reply.split():
        if not tok.isdigit() or not 1 <= int(tok) <= count:
            raise ValueError(tok)
        picked.append(int(tok))
    return picked


def menu_pick(items: list[str], noun: str, ask: Callable[[str], str],
              out=None) -> list[str]:
    """Numbered menu: print items to out, ask until an answer names some."""
    out = out or sys.stderr
    print_menu(items, out)
    while True:
        try:
            reply = ask(f"{noun} [1-{len(items)}, e.g. 1 3, a=all, q]: ")
        except EOFError:
            raise Abort from None
        try:
            picked = parse_menu_reply(reply, len(items))
        except ValueError as e:
            print(f"  not a choice: {e}", file=out)
            continue
        if picked is None:
            raise Abort
        if not picked:
            return list(items)
        return [items[n - 1] for n in picked]


def tty_ask(prompt: str) -> str:
    """Prompt on the controlling terminal, so stdin can stay a pipe."""
    with open("/dev/tty", "w") as out, open("/dev/tty") as inp:
        out.write(prompt)
        out.flush()
        line = inp.readline()
    if not line:
        raise EOFError
    return line.rstrip("\n")


def pick(items: Iterable[str], noun: str) -> list[str]:
    """Marked subset of items; fzf on a tty when installed, else the menu."""
    items = list(items)
    if fzf_usable():
        return fzf_pick(items, noun)
    return menu_pick(items, noun, tty_ask)


def choose(items: list[str], prompt: str, header: str = "",
           free_text: bool = False, query: str = "") -> str | None:
    """One of items, the single-choice counterpart of pick(); None when the
    user leaves. sshtsf's menus, which ask on stdin like its other prompts.

    free_text allows a value that is not in the list -- right for a remote
    folder, wrong for a host or session, where anything off-list is a typo and
    would only fail a lookup later. query pre-fills fzf's search box.
    """
    if not items:
        return None
    if fzf_usable():
        cmd = fzf_argv(prompt + " ", header)
        if query:
            cmd += ["--query", query]
        if free_text:
            # print-query puts the typed text on line 1 and any match after it,
            # so a value with no match still comes back.
            cmd += ["--print-query"]
        proc = subprocess.run(cmd, input="\n".join(items), text=True,
                              stdout=subprocess.PIPE)
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        # Prefer a real selection; with free_text fall back to the query when
        # nothing matched. Exit 130 is abort; exit 1 with a query is "no match".
        if proc.returncode == 130 or (proc.returncode != 0 and not free_text):
            return None
        return lines[-1] if lines else None
    return choose_numbered(items, prompt, header, free_text)


MENU_LIMIT = 40


def choose_numbered(items: list[str], prompt: str, header: str = "",
                    free_text: bool = False) -> str | None:
    """choose() without fzf: a numbered menu of the first MENU_LIMIT items."""
    if header:
        print(header, file=sys.stderr)
    shown = items[:MENU_LIMIT]
    print_menu(shown, sys.stderr)
    if len(items) > len(shown):
        note = "type a value to use it" if free_text else "install fzf to filter"
        print(f"  ... {len(items) - len(shown)} more ({note})", file=sys.stderr)

    hint = f"1-{len(shown)}, a value, or q" if free_text else f"1-{len(shown)}, or q"
    while True:
        try:
            reply = input(f"{prompt} [{hint}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return None
        if reply in ("q", "Q", ""):
            return None
        if reply.isdigit() and 1 <= int(reply) <= len(shown):
            return shown[int(reply) - 1]
        if reply in items or free_text:
            return reply
        print(f"  not a choice: {reply}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pick-lines", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("noun", help='what the items are, for the prompts ("hosts")')
    parser.add_argument("item", nargs="+")
    args = parser.parse_args(argv)
    try:
        sel = pick(args.item, args.noun)
    except Abort:
        return EXIT_ABORT
    print("\n".join(sel))
    return 0


if __name__ == "__main__":
    sys.exit(main())
