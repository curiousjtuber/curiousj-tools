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


def fzf_pick(items: list[str], noun: str) -> list[str]:
    """Run fzf over items; its UI goes to the tty, the marked lines come back."""
    proc = subprocess.run(
        ["fzf", "--multi", "--prompt", f"{noun}> ", "--height", "60%", "--reverse",
         "--header", f"TAB marks {noun}, ENTER confirms"],
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
    for i, item in enumerate(items, 1):
        print(f"  {i:2d}) {item}", file=out)
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
    if shutil.which("fzf") and sys.stdin.isatty():
        return fzf_pick(items, noun)
    return menu_pick(items, noun, tty_ask)


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
