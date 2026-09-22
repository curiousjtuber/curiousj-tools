"""Attributes on logins and paths, and the conditions that select by them.

An entry's attributes are a list of strings, each a flag or a key with a
value:

    attributes = ["mise", "cachyos", "arch=x86_64"]

A condition (an operation's `logins` or `paths`, the -a and -A flags) is
made of terms:

    mise            the attribute is there
    arch=x86_64     ...with that value
    !mise           it is not there
    arch!=x86_64    it is not there, or has another value

and is a term, a list of terms (every one has to hold), or a table with any
of `all`, `any` and `none`, each a term or a list of them:

    logins = "mise"
    logins = ["mise", "cachyos"]
    logins = { any = ["mac", "brew"], none = "headless" }

`true`, an empty list or an empty table match everything. Parsing errors
are ValueError with the whole message; the lists reader turns them into
its own error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

Attributes = dict[str, str | None]  # a flag's value is None


@dataclass(frozen=True)
class Term:
    key: str
    value: str | None = None  # None: presence is what counts
    negated: bool = False

    def holds(self, attrs: Mapping[str, str | None]) -> bool:
        hit = self.key in attrs if self.value is None else attrs.get(self.key) == self.value
        return hit != self.negated

    def __str__(self) -> str:
        if self.value is None:
            return ("!" if self.negated else "") + self.key
        return self.key + ("!=" if self.negated else "=") + self.value


@dataclass(frozen=True)
class Condition:
    all: tuple[Term, ...] = ()
    any: tuple[Term, ...] = ()
    none: tuple[Term, ...] = ()

    def matches(self, attrs: Mapping[str, str | None]) -> bool:
        return (all(t.holds(attrs) for t in self.all)
                and (not self.any or any(t.holds(attrs) for t in self.any))
                and not any(t.holds(attrs) for t in self.none))

    def __str__(self) -> str:
        """The terms as written: the all-terms bare, `any`/`none` named."""
        parts = [(name + " " if name else "") + " ".join(map(str, terms))
                 for name, terms in (("", self.all), ("any", self.any), ("none", self.none))
                 if terms]
        return "; ".join(parts) if parts else "everything"


EVERYTHING = Condition()


def matches(cond: Condition, attrs: Mapping[str, str | None]) -> bool:
    return cond.matches(attrs)


def holds_all(terms: tuple[Term, ...], attrs: Mapping[str, str | None]) -> bool:
    """Whether every term holds: what a list of -a or -A flags asks."""
    return all(t.holds(attrs) for t in terms)


def term(text: Any, where: str) -> Term:
    """One term as written. Raises ValueError naming where it was found."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{where}: a term is a string: key, key=value, !key or key!=value")
    raw = text.strip()
    negated = raw.startswith("!")
    body = raw[1:] if negated else raw
    key, eq, value = body.partition("=")
    if eq and key.endswith("!"):
        if negated:
            raise ValueError(f"{where}: {raw!r}: one negation, key!=value")
        key, negated = key[:-1], True
    elif eq and negated:
        raise ValueError(f"{where}: {raw!r}: write key!=value, not !key=value")
    check_key(key, raw, where)
    if eq:
        if not value:
            raise ValueError(f"{where}: {raw!r}: a value after =")
        if "=" in value:
            raise ValueError(f"{where}: {raw!r}: one = at most")
    return Term(key, value if eq else None, negated)


def check_key(key: str, raw: str, where: str) -> None:
    if not key or any(c.isspace() for c in key) or "!" in key or "=" in key:
        raise ValueError(f"{where}: {raw!r}: a key is a word without spaces, = or !")


def terms(raw: Any, where: str) -> tuple[Term, ...]:
    """A term, or a list of terms."""
    if isinstance(raw, list):
        return tuple(term(t, where) for t in raw)
    return (term(raw, where),)


def condition(raw: Any, where: str) -> Condition:
    """A condition as the module docstring shows it; True matches everything."""
    if raw is True or raw is None:
        return EVERYTHING
    if isinstance(raw, (str, list)):
        return Condition(all=terms(raw, where))
    if isinstance(raw, dict):
        for key in raw:
            if key not in ("all", "any", "none"):
                raise ValueError(f"{where}: unknown key {key!r} in a condition; known: all, any, none")
        return Condition(**{key: terms(value, where) for key, value in raw.items()})
    raise ValueError(f"{where}: a condition is a term, a list of terms, or a table with "
                     "all, any, none (or true for everything)")


def attributes(raw: Any, where: str) -> Attributes:
    """An entry's attributes: a list of `key` or `key=value` strings."""
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise ValueError(f"{where}: attributes has to be a list of key or key=value strings")
    out: Attributes = {}
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{where}: attributes has to be a list of key or key=value strings")
        text = item.strip()
        key, eq, value = text.partition("=")
        check_key(key, text, where)
        if eq and (not value or "=" in value):
            raise ValueError(f"{where}: {text!r}: one = at most, with a value after it")
        if key in out:
            raise ValueError(f"{where}: attribute {key!r} given twice")
        out[key] = value if eq else None
    return out
