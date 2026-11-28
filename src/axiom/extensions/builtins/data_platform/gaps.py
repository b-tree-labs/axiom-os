# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What a site would need to declare, ordered by how much it would fix.

A steward cannot declare what they cannot see. The detection for most of this
already exists and is scattered: map coverage in one module, tier shape in
another, key collisions in a third, unit counts in a query somebody ran once.
Every gap found on the live install this week was found because a person went
looking, which is not a process.

**This composes rather than re-derives.** It holds the existing checks over one
site's facts and returns one ordered list, and every entry names the
declaration that would close it. A steward should meet the list; they should
not discover it.

**Ordered by rows affected, not by severity.** A severity is a judgement we
would be making on a site's behalf, and it is the wrong judgement often enough
to be worse than none: 19.1 million unitless rows and three unitless rows are
the same defect and nowhere near the same problem. The count is a fact and it
sorts correctly without anyone grading anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: What kind of act closes a gap. These are not severities and they are not
#: interchangeable: ADR-042 D10 is explicit that a correction and a retraction
#: are different primitives, and bundling them behind one word is how a
#: steward ends up asserting something false.
#:
#: A **correction** says the value was wrong and here is the right one. A
#: **retraction** says stop deriving from this, while the record persists for
#: audit — it does not claim the values were wrong. Retiring a channel that
#: has never varied is a retraction: nothing about its 696,231 rows is
#: incorrect, and declaring a replacement value would assert something false
#: about every one of them.
NEEDS_DECLARATION = "declaration"
NEEDS_RETRACTION = "retraction"
#: Neither. The values are right and the KIND is wrong — a model mesh stored
#: as a reading is configuration, not a bad measurement. ADR-042 leaves this
#: open as possibly a third verb, and it is named here rather than forced
#: into one of the other two.
NEEDS_RECLASSIFICATION = "reclassification"
NEEDS_PRODUCER = "producer"
NEEDS_DECISION = "decision"


@dataclass(frozen=True)
class Gap:
    """One thing that is wrong, and the specific act that would fix it."""

    kind: str
    site: str
    rows: int
    summary: str
    #: The act. Not "add a unit" but which channels, in which file.
    fix: str
    #: What closing it would DO, in rows and direction. A proposal without a
    #: blast radius reads as safe, and "withholds 46,662 readings" and "adds a
    #: unit to 3,430,514 rows" are opposite kinds of change wearing the same
    #: word.
    effect: str = ""
    needs: str = NEEDS_DECLARATION
    detail: tuple[str, ...] = field(default_factory=tuple)

    @property
    def actionable_by_steward(self) -> bool:
        return self.needs == NEEDS_DECLARATION


def assess(
    site: str,
    *,
    unitless: dict[str, int] | None = None,
    roleless: dict[str, int] | None = None,
    fault_values: dict[str, int] | None = None,
    constant: dict[str, int] | None = None,
    colliding_instants: int = 0,
    colliding_divergent: int = 0,
    unmapped_refs: dict[str, int] | None = None,
) -> list[Gap]:
    """One site's gaps, worst first.

    Each argument is a fact somebody measured, so this stays a pure function
    and the expensive part lives in the skill that gathers them.
    """
    gaps: list[Gap] = []

    unitless = unitless or {}
    if unitless:
        gaps.append(Gap(
            kind="no unit",
            site=site,
            rows=sum(unitless.values()),
            summary=(
                f"{len(unitless)} channel(s) arrive with no unit, across "
                f"{sum(unitless.values()):,} rows. A value with no unit is "
                "served as a bare number, and a bare number reads as a fact."
            ),
            fix=(
                "add `unit = \"...\"` for these channels in the site's channel "
                "map, then `axi data rederive --site " + site + "` to apply it "
                "to the rows already stored"
            ),
            effect=(
                f"adds a unit to {sum(unitless.values()):,} row(s). Nothing is "
                "withheld and no value changes."
            ),
            detail=_top(unitless),
        ))

    roleless = roleless or {}
    if roleless:
        gaps.append(Gap(
            kind="no role",
            site=site,
            rows=sum(roleless.values()),
            summary=(
                f"{len(roleless)} channel(s) carry no role, so nothing here can "
                "be compared with the same quantity at another site. A channel "
                "name is a local label; a role is the only portable part."
            ),
            fix=(
                "add `role = \"...\"` for these channels in the site's channel "
                "map. Reuse an existing role where one fits rather than "
                "inventing a name, since a role that nobody else uses joins "
                "nothing"
            ),
            effect=(
                f"makes {sum(roleless.values()):,} row(s) comparable with the "
                "same quantity elsewhere. No value changes."
            ),
            detail=_top(roleless),
        ))

    fault_values = fault_values or {}
    if fault_values:
        gaps.append(Gap(
            kind="fault codes served",
            site=site,
            rows=sum(fault_values.values()),
            summary=(
                f"{sum(fault_values.values()):,} reading(s) sit at a value that "
                "looks like a hardware fault word rather than a measurement. "
                "They are being averaged and charted as data."
            ),
            fix=(
                "declare the value each device emits when it cannot read, so "
                "those readings are withheld instead of served"
            ),
            effect=(
                f"WITHHOLDS {sum(fault_values.values()):,} reading(s) that are "
                "currently served and averaged. Every chart and mean over these "
                "channels will change."
            ),
            detail=_top(fault_values),
        ))

    constant = constant or {}
    if constant:
        gaps.append(Gap(
            kind="never varies",
            site=site,
            rows=sum(constant.values()),
            summary=(
                f"{len(constant)} channel(s) have never changed value. Nothing "
                "about those rows is incorrect, which is why this is not a "
                "correction."
            ),
            fix=(
                "three different operations, and only a person knows which. "
                "RETRACT it if the channel should stop being served but the "
                "record should persist (ADR-042 D10: a retraction does not "
                "claim the values were wrong). RECLASSIFY it if the values are "
                "right and it is configuration rather than a reading. Or fix "
                "the source if the sensor is stuck. Declaring a replacement "
                "value would assert something false about every row"
            ),
            needs=NEEDS_RETRACTION,
            effect=(
                "a retraction stops forward derivation and changes no value; "
                "the rows remain for audit"
            ),
            detail=_top(constant),
        ))

    unmapped_refs = unmapped_refs or {}
    if unmapped_refs:
        gaps.append(Gap(
            kind="no channel map",
            site=site,
            rows=sum(unmapped_refs.values()),
            summary=(
                f"{len(unmapped_refs)} schema_ref(s) have no channel map at "
                "all, so nothing about their channels can be declared. This is "
                "upstream of every other gap on this list."
            ),
            fix=(
                "add a channel map declaring each ref, or declare the ref as a "
                "former name of a map that already exists if the feed was "
                "renamed"
            ),
            effect=(
                f"makes {sum(unmapped_refs.values()):,} row(s) declarable at "
                "all. On its own it changes nothing; it unblocks the rest."
            ),
            detail=_top(unmapped_refs),
        ))

    if colliding_instants:
        gaps.append(Gap(
            kind="timestamp collisions",
            site=site,
            rows=colliding_instants,
            summary=(
                f"{colliding_instants:,} instant(s) carry more than one row, "
                f"{colliding_divergent:,} of them with different values. A "
                "point-in-time answer over these is a coin toss."
            ),
            fix=(
                "the producer's timestamps cannot separate two readings. Either "
                "emit finer timestamps, or accept that this channel has no "
                "unique key and say so at the surface. Do NOT deduplicate: on "
                "one channel zero of 370,889 collisions were re-sends, so "
                "collapsing them deletes real readings"
            ),
            needs=NEEDS_PRODUCER,
            effect=(
                "nothing we can do here changes it. A dedupe would DELETE real "
                "readings rather than fix anything"
            ),
        ))

    return sorted(gaps, key=lambda g: (-g.rows, g.kind))


def _top(counts: dict[str, int], limit: int = 6) -> tuple[str, ...]:
    """The worst few by name, so a fix has somewhere to start."""
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    out = [f"{name} ({n:,})" for name, n in ordered[:limit]]
    if len(ordered) > limit:
        out.append(f"and {len(ordered) - limit} more")
    return tuple(out)


def report(site: str, gaps: list[Gap]) -> list[str]:
    """The steward's list, worst first."""
    if not gaps:
        return [f"{site}: nothing to declare — every channel carries a unit and a role"]
    lines = [f"{site}: {len(gaps)} gap(s), worst first", ""]
    for gap in gaps:
        mark = {
            NEEDS_DECLARATION: "declare",
            NEEDS_RETRACTION: "retract",
            NEEDS_RECLASSIFICATION: "reclassify",
            NEEDS_PRODUCER: "producer",
            NEEDS_DECISION: "decide",
        }[gap.needs]
        lines.append(f"  [{mark}] {gap.kind} — {gap.rows:,} row(s)")
        lines.append(f"      {gap.summary}")
        if gap.detail:
            lines.append(f"      {', '.join(gap.detail)}")
        lines.append(f"      fix: {gap.fix}")
        if gap.effect:
            lines.append(f"      effect: {gap.effect}")
        lines.append("")
    steward = [g for g in gaps if g.actionable_by_steward]
    lines.append(
        f"  {len(steward)} of {len(gaps)} can be closed by declaring something; "
        "the rest need a producer change or a decision about the data"
    )
    return lines


__all__ = [
    "NEEDS_DECISION",
    "NEEDS_DECLARATION",
    "NEEDS_PRODUCER",
    "NEEDS_RECLASSIFICATION",
    "NEEDS_RETRACTION",
    "Gap",
    "assess",
    "report",
]
