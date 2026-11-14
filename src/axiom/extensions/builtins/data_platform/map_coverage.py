# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Does a channel map describe the data it claims to describe?

A map can be complete, correct, reviewed, and match **nothing**. Measured on a
live install: one site's map declared 21 channels, every one of them carrying a
unit; that site had 21 channels in silver with no unit; and the overlap between
the two sets was **zero**. The map said ``TW_01``, the data said ``TW_1``.

Nothing anywhere would have told you. The map looks right on its own. The data
looks unitless on its own. Only comparing them says anything, and until now
nothing did — so the map had been sitting there describing an empty set while
496,923 rows went out with no unit.

The same install had a second version of the same failure one level up: a map
declaring ``schema_ref = "site-b/mat-v1"`` while every row carried
``site-a/mat-v1``. A map keyed to a ref nothing uses is a map for
nobody.

**Near misses are the payload.** "You declared 21 channels and none of them
matched" is a report. "You declared ``TW_01`` and the data has ``TW_1``" is a
fix. The suggestion is never applied automatically — a name that looks like a
typo is sometimes two real instruments — but it turns an invisible no-op into
a one-line correction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Everything that is decoration in a channel name: case, separators, and the
#: zero padding that makes ``TW_01`` and ``TW_1`` different strings and the
#: same instrument.
_DECORATION = re.compile(r"[^a-z0-9]+")
_LEADING_ZEROS = re.compile(r"(?<!\d)0+(\d)")


def normalize(name: str) -> str:
    """A channel name with its decoration removed, for near-miss matching only.

    Never for lookup. Two genuinely different channels can normalize to the
    same string, which is exactly why this proposes rather than applies.
    """
    bare = _DECORATION.sub("", str(name).strip().lower())
    return _LEADING_ZEROS.sub(r"\1", bare)


@dataclass(frozen=True)
class Coverage:
    """One schema_ref's map, held against the data it claims to describe."""

    schema_ref: str
    #: Declared and present, with a unit. The working case.
    matched_with_unit: tuple[str, ...] = ()
    #: Declared and present, but the map gives no unit. The data will land
    #: unitless and the map will look like it covered it.
    matched_without_unit: tuple[str, ...] = ()
    #: In the map, never in the data. Either the producer stopped sending it
    #: or the name is wrong.
    declared_never_seen: tuple[str, ...] = ()
    #: In the data, not in the map. These are the rows that go out unitless.
    seen_never_declared: tuple[str, ...] = ()
    #: ``(declared, observed)`` pairs that differ only in decoration.
    near_misses: tuple[tuple[str, str], ...] = ()
    #: Rows carrying this ref, and how many of them lack a unit.
    rows: int = 0
    unitless_rows: int = 0

    @property
    def matched(self) -> int:
        return len(self.matched_with_unit) + len(self.matched_without_unit)

    @property
    def verdict(self) -> str:
        if not self.rows and self.declared_never_seen:
            # Vacuously true is not true. An empty observed set made this
            # report "the map covers the data", which is the exact shape of
            # green-that-cannot-fail this module exists to find — and it read
            # that way for the two maps keyed to a schema_ref nothing uses.
            return (
                f"no rows carry this schema_ref, so nothing was compared. The "
                f"map declares {len(self.declared_never_seen)} channel(s) for "
                "data that does not exist under this ref"
            )
        if self.near_misses:
            pairs = ", ".join(f"{d} → {o}" for d, o in self.near_misses[:3])
            more = "" if len(self.near_misses) <= 3 else f" (+{len(self.near_misses) - 3} more)"
            return (
                f"the map and the data differ only in how the names are "
                f"written: {pairs}{more}. Nothing matched, so the map covered "
                "none of this data."
            )
        if not self.matched and self.seen_never_declared:
            return (
                f"the map matched none of the {len(self.seen_never_declared)} "
                "channel(s) actually arriving under this schema_ref"
            )
        if self.matched_without_unit:
            return (
                f"{len(self.matched_without_unit)} channel(s) are declared but "
                "without a unit, so they land unitless while the map looks "
                "like it covered them"
            )
        if self.seen_never_declared:
            return f"{len(self.seen_never_declared)} channel(s) arrive with no declaration"
        return "the map covers the data"

    @property
    def healthy(self) -> bool:
        if not self.rows and self.declared_never_seen:
            return False
        return not (
            self.seen_never_declared or self.matched_without_unit or self.near_misses
        )


def compare(
    schema_ref: str,
    declared: dict[str, str | None],
    observed: dict[str, int],
    *,
    unitless: dict[str, int] | None = None,
) -> Coverage:
    """Hold one map against one schema_ref's data.

    ``declared`` maps a channel name to its unit (``None`` or ``""`` when the
    map names the channel but gives no unit — a distinction that matters,
    because a declared channel with no unit looks covered and is not).
    ``observed`` maps a channel name to its row count.
    """
    unitless = unitless or {}
    declared_names = set(declared)
    observed_names = set(observed)

    both = declared_names & observed_names
    with_unit = tuple(sorted(c for c in both if str(declared.get(c) or "").strip()))
    without_unit = tuple(sorted(c for c in both if not str(declared.get(c) or "").strip()))
    only_declared = sorted(declared_names - observed_names)
    only_observed = sorted(observed_names - declared_names)

    # Near misses among the two leftovers only. A declared channel that DID
    # match is not a near miss for anything.
    by_norm: dict[str, str] = {}
    for name in only_observed:
        by_norm.setdefault(normalize(name), name)
    misses = tuple(
        (name, by_norm[normalize(name)])
        for name in only_declared
        if normalize(name) in by_norm
    )

    return Coverage(
        schema_ref=schema_ref,
        matched_with_unit=with_unit,
        matched_without_unit=without_unit,
        declared_never_seen=tuple(only_declared),
        seen_never_declared=tuple(only_observed),
        near_misses=misses,
        rows=sum(observed.values()),
        unitless_rows=sum(unitless.values()),
    )


@dataclass
class RefMismatch:
    """A map keyed to a schema_ref nothing uses, or data no map claims."""

    declared_refs: tuple[str, ...] = ()
    observed_refs: tuple[str, ...] = ()
    near_misses: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def maps_for_nobody(self) -> tuple[str, ...]:
        return tuple(r for r in self.declared_refs if r not in self.observed_refs)

    @property
    def data_with_no_map(self) -> tuple[str, ...]:
        return tuple(r for r in self.observed_refs if r not in self.declared_refs)


def compare_refs(declared_refs, observed_refs) -> RefMismatch:
    """The same question one level up: is the map even keyed to this data?

    A map declaring ``site-a/mat-v1`` against rows carrying
    ``site-b/mat-v1`` covers nothing, and the two differ by a site rename
    that happened to one of them.
    """
    declared = tuple(sorted(set(declared_refs)))
    observed = tuple(sorted(set(observed_refs)))
    by_norm = {normalize(r): r for r in observed if r not in declared}
    misses = tuple(
        (r, by_norm[normalize(r)])
        for r in declared
        if r not in observed and normalize(r) in by_norm
    )
    return RefMismatch(declared_refs=declared, observed_refs=observed, near_misses=misses)


@dataclass(frozen=True)
class JoinGap:
    """A channel that cannot be compared with anything, and why."""

    schema_ref: str
    channel: str
    reason: str
    candidates: tuple[str, ...] = ()


def roleless(declarations: dict[str, dict[str, dict[str, str]]]) -> list[JoinGap]:
    """Declared channels carrying no role.

    A role is what says two channels mean the same quantity. Without one a
    channel is unjoinable **by construction** — not by accident, and not
    recoverably by anyone downstream — so it can never be compared with its
    own counterpart on another feed, let alone another site.

    This is the complete half of the check. Everything below it is a
    heuristic; this is exhaustive.
    """
    out: list[JoinGap] = []
    for ref, channels in sorted(declarations.items()):
        for channel, body in sorted(channels.items()):
            if not str(body.get("role") or "").strip():
                out.append(
                    JoinGap(ref, channel, "no role, so nothing can be compared with it")
                )
    return out


def unjoined_lookalikes(
    declarations: dict[str, dict[str, dict[str, str]]],
) -> list[JoinGap]:
    """Channels on different feeds that read like the same quantity and
    share no role.

    The case this was written from: one site carried `ChanA1`,
    `chan_a_1` and `ChanA1` again across three feeds, and a question
    about that quantity had three unrelated answers depending on which
    feed somebody queried.

    **This finds a minority of them and that is the point worth knowing.**
    Against that site's real vocabularies it matched three of eleven —
    `Power`/`power` and two others — and missed every abbreviated
    name, because `XR` and `long_name_for_xr` are not a spelling difference.
    Abbreviation to word is not a string problem, and no normaliser will make
    it one. A role is declared, not inferred; this only shortens the list
    somebody has to work through.
    """
    by_norm: dict[str, list[tuple[str, str, str]]] = {}
    for ref, channels in declarations.items():
        for channel, body in channels.items():
            role = str(body.get("role") or "").strip()
            by_norm.setdefault(normalize(channel), []).append((ref, channel, role))

    out: list[JoinGap] = []
    for entries in by_norm.values():
        refs = {ref for ref, _c, _r in entries}
        if len(refs) < 2:
            continue
        roles = {role for _ref, _c, role in entries if role}
        if len(roles) == 1 and all(role for _r, _c, role in entries):
            continue  # already joined, and joined consistently
        for ref, channel, role in sorted(entries):
            reason = (
                "reads like a channel on another feed but carries no role"
                if not role
                else f"reads like a channel on another feed that carries a different role ({role})"
            )
            out.append(
                JoinGap(
                    ref, channel, reason,
                    candidates=tuple(
                        f"{r}:{c}" for r, c, _ in sorted(entries) if (r, c) != (ref, channel)
                    ),
                )
            )
    return sorted(out, key=lambda g: (g.schema_ref, g.channel))


def joined_by_role(
    declarations: dict[str, dict[str, dict[str, str]]],
) -> dict[str, tuple[str, ...]]:
    """``role -> the channels that answer to it``, across every feed.

    The positive report. A role naming channels on one feed only is a role
    doing no joining yet, which is worth seeing beside the ones that are.
    """
    out: dict[str, list[str]] = {}
    for ref, channels in declarations.items():
        for channel, body in channels.items():
            role = str(body.get("role") or "").strip()
            if role:
                out.setdefault(role, []).append(f"{ref}:{channel}")
    return {role: tuple(sorted(members)) for role, members in sorted(out.items())}


__all__ = ["Coverage", "RefMismatch", "compare", "compare_refs", "normalize"]
