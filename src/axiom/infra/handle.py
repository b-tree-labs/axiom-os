# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Parsing and joining `@name:context` handles (ADR-020).

A handle names a principal: `@name`, or `@name:context` when it matters where
they were standing. The grammar is asserted across the codebase and in ADR-020,
but nothing parsed it — so identity comparisons were string equality, and a
person who moved context became a different person and lost their history.

WHY NAME-ONLY MATCHING IS NOT THE FIX. The obvious repair is to compare the
name halves and call it done. That is unsafe: `@bsmith:northlab` and
`@bsmith:netl` may be two different humans, and the context is exactly what
distinguished them. Merging on the name alone would hand one person another
person's memory — a worse bug than the one being fixed.

So a join across contexts requires an ALIAS SET the node has attested: an
identity provider's subject binds the handles that are genuinely the same
human. Absent that attestation, comparison stays strict. Portability is a
property the node vouches for, never one inferred from spelling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class Handle:
    """A parsed `@name:context`."""

    name: str
    context: str = ""

    @property
    def canonical(self) -> str:
        """The handle as written, normalised."""
        return f"@{self.name}:{self.context}" if self.context else f"@{self.name}"

    @property
    def person(self) -> str:
        """The context-free form — the person, wherever they were standing.

        NOT a join key on its own; see the module docstring. Two people can
        share a name across contexts.
        """
        return f"@{self.name}"

    def __str__(self) -> str:
        return self.canonical


def parse_handle(value: str) -> Handle:
    """Parse `@name` or `@name:context`.

    Case-insensitive: handles are identities, and a client that lower-cases
    an address must not create a second person.
    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError("handle is empty")
    if raw.startswith("@"):
        raw = raw[1:]
    if raw.startswith("@"):
        raise ValueError(f"handle has more than one leading '@': {value!r}")
    if raw.count(":") > 1:
        raise ValueError(f"handle has more than one context separator: {value!r}")

    name, _, context = raw.partition(":")
    name, context = name.strip().lower(), context.strip().lower()
    if not _PART.match(name):
        raise ValueError(f"handle name is not well formed: {value!r}")
    if context and not _PART.match(context):
        raise ValueError(f"handle context is not well formed: {value!r}")
    return Handle(name=name, context=context)


def try_parse_handle(value: str) -> Handle | None:
    """Parse, or ``None`` — for comparing values that may not be handles."""
    try:
        return parse_handle(value)
    except ValueError:
        return None


def same_principal(left: str, right: str, aliases: set[str] | None = None) -> bool:
    """Whether two handles denote the same principal.

    Strict by default: identical handles only. Widening happens ONLY through
    *aliases*, a set of handles the node has attested to be the same human —
    typically derived from one identity-provider subject.

    The alias set is checked as a whole rather than pairwise, so a caller
    cannot widen a comparison by asserting a single side.
    """
    first, second = try_parse_handle(left), try_parse_handle(right)
    if first is None or second is None:
        return False
    if first.canonical == second.canonical:
        return True
    if not aliases:
        return False

    attested = {
        parsed.canonical
        for parsed in (try_parse_handle(a) for a in aliases)
        if parsed is not None
    }
    # Both sides must be attested. Otherwise an unknown handle would join a
    # known one just by being asked about alongside it.
    return first.canonical in attested and second.canonical in attested


def person_key(value: str, aliases: set[str] | None = None) -> str:
    """A stable key for grouping one principal's records.

    With an attested alias set, every alias collapses to one key so a person's
    history is reachable whichever context they were wearing. Without one, the
    key is the full handle — strict, and no two people are merged by accident.
    """
    parsed = try_parse_handle(value)
    if parsed is None:
        return (value or "").strip().lower()
    if not aliases:
        return parsed.canonical

    attested = sorted(
        parsed_alias.canonical
        for parsed_alias in (try_parse_handle(a) for a in aliases)
        if parsed_alias is not None
    )
    if parsed.canonical in attested:
        # The lexically-first attested handle is the group's representative:
        # stable, order-independent, and derived from the set rather than
        # from whichever handle happened to be asked about first.
        return attested[0]
    return parsed.canonical
