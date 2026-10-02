# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The medallion rule as data, so it can be checked instead of remembered.

ADR-128. Everyone stated the rule and nothing enforced it, so it had drifted
in both directions: two serving paths read ``silver.signals`` directly, and
the same KIND of data — authored reference records that never had a raw form —
sat in silver in one case and gold in another with nothing to say which was
right.

**Transformation lives in silver.** Bronze receives, silver transforms, gold
publishes. Bronze does not transform, because a change made on the way in is a
change nobody can audit against an original that no longer exists. Gold does
not transform either — it *derives*: select, filter, join, rename, bucket,
aggregate, all of which change the shape of an answer without changing what a
record means. Cleaning, hydrating, resolving an alias, fixing a unit and
repairing a timestamp all change what a record means, so they are silver's.

The tier is chosen by what must HAPPEN to the data, not by where it came
from. Bytes whose shape is not yet trusted are bronze. Records needing any of
:data:`SILVER_TRANSFORMS` before anyone relies on them are silver. Records
that are ready to be relied on as authored are gold. A hand-authored
calibration curve belongs in gold even though it never saw bronze; forcing it
through two tiers invents a raw landing that does not exist and a conform step
that does nothing.

The allowances below are ADR-128's named exceptions — "allowance" because
"exception" is already three other things in a Python codebase. Each is a
category, not a table: a new table joins an existing category or it is not an
allowance.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

#: Ordered from raw to served.
TIERS = ("bronze", "silver", "gold")

#: The only tier a serving path reads. "Serving" means anything whose output
#: reaches a person, an agent, an API, a figure or a report — a count for a
#: status line is serving, and so is a chart's provenance string.
SERVING_TIER = "gold"

#: The tiers a serving path must not read. Named rather than derived so the
#: guard that greps for them reads the same list this module publishes.
WORKING_TIERS = ("bronze", "silver")


#: What silver does. Exhaustive on purpose: "the working tier" is not a
#: definition anyone can check, and this is. A record needing any of these
#: belongs in silver; a gold object doing any of these is silver's work done
#: in the wrong place, and the next view that needs it will do it differently.
SILVER_TRANSFORMS: tuple[tuple[str, str], ...] = (
    ("conform", "one shape, one unit vocabulary, one timestamp convention"),
    ("clean", "sentinels, NaNs, out-of-range values, stray whitespace, naive timestamps"),
    ("cast", "strings to numbers, numbers to the declared type, timestamps to aware"),
    ("decompose", "a landed artifact into the atomic records it actually contains"),
    (
        "hydrate",
        "what the producer referenced but did not carry: a channel's role and unit "
        "from the site map, a connector's site, a code's label",
    ),
    (
        "resolve identity",
        "a former name becomes the canonical one; skipping this is how one site "
        "became two",
    ),
    (
        "deduplicate",
        "one canonical row per natural key, by idempotent upsert, so a replayed "
        "batch converges instead of accumulating",
    ),
    (
        "compose",
        "atomic records assembled into the joinable entities gold derives from: an "
        "observation with its identity, a run with its segments",
    ),
    (
        "validate",
        "refuse what cannot be admitted, and record why where the producer can see it",
    ),
    (
        "stamp provenance",
        "source_class, schema_ref, row_hash, quality, arrival time; measured and "
        "modelled are told apart here or nowhere",
    ),
    (
        "reconcile",
        "late arrivals and corrections land as a correction rather than a second truth",
    ),
    ("stage", "the result sits addressable until gold derives from it"),
)

#: What gold does instead. Shape, not meaning.
GOLD_DERIVATIONS: tuple[str, ...] = (
    "select",
    "filter",
    "join",
    "rename",
    "bucket",
    "aggregate",
)


@dataclass(frozen=True)
class Allowance:
    """One named departure from bronze → silver → gold."""

    code: str
    title: str
    rule: str


ALLOWANCES: dict[str, Allowance] = {
    "E1": Allowance(
        "E1",
        "authored reference data",
        "Written directly to the tier it is ready for. A calibration curve, a "
        "core configuration, an operator roster: it has no raw form, so it has "
        "no bronze. Ready to serve means gold; needing conforming or versioning "
        "first means silver.",
    ),
    "E2": Allowance(
        "E2",
        "curator annotations",
        "Written directly to silver. Run labels and segment boundaries, "
        "authored by a person ABOUT records already in silver. They are joined "
        "on silver's keys, so they are silver-shaped by construction, and they "
        "are not served directly — gold serves the join.",
    ),
    "E3": Allowance(
        "E3",
        "analysis outputs",
        "Written to the tier their consumers need. A correction parameter set, "
        "an investigation finding. An analysis output is authored data with a "
        "computation behind it, so the same test applies as E1.",
    ),
    "E4": Allowance(
        "E4",
        "the tools that maintain a tier",
        "The conform pass, the schema manager that creates silver's columns and "
        "the gold views over them, the rename migration, the backup path. They "
        "are the tier's implementation, not its consumers.",
    ),
    "E5": Allowance(
        "E5",
        "a diagnostic that is explicitly not serving",
        "May read any tier, provided it says which tier it read. Refusing to "
        "let an operator look at bronze while debugging an ingest is the rule "
        "defeating its own purpose. The condition is that its output is not "
        "presented as an answer ABOUT the data.",
    ),
}

#: Not allowances, and each has already been argued for once:
#:
#: - Convenience. "Silver already has it and gold is only a view over silver"
#:   is the argument that produced both drifts.
#: - Performance. A slow gold view is fixed or indexed; reading around it
#:   hides the problem and keeps it.
#: - A missing gold object. If serving needs something gold does not expose,
#:   expose it.
NOT_ALLOWANCES = ("convenience", "performance", "a missing gold object")

#: Gold base tables this install declares as authored reference or analysis
#: data (E1/E3). Format matches ``AXIOM_SITE_IDENTITIES``' spirit: a plain
#: comma-separated list of bare table names.
#:
#: The default is empty on purpose. Axiom writes no gold base tables at all —
#: every one that exists on a node was put there by a consumer or by hand, so
#: the platform cannot declare them and should not pretend to. What it CAN do
#: is notice they are there.
DECLARATION_ENV = "AXIOM_GOLD_REFERENCE_TABLES"


def declared_gold_tables(environ: dict[str, str] | None = None) -> frozenset[str]:
    raw = (environ if environ is not None else os.environ).get(DECLARATION_ENV, "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Finding:
    """Something in the database the rule did not expect."""

    schema: str
    name: str
    kind: str
    detail: str


def classify(
    objects: list[tuple[str, str, str]],
    declared: frozenset[str] = frozenset(),
) -> list[Finding]:
    """Findings over ``(schema, name, object_type)`` rows.

    ``object_type`` is ``information_schema.tables.table_type``: ``BASE TABLE``
    or ``VIEW``.

    Only one shape is reported, because only one is a surprise: a base table in
    gold. Gold is meant to be views over silver plus the reference data
    somebody declared, so an undeclared base table there is either an E1/E3
    record nobody wrote down or a serving surface that quietly stopped being
    derived. Both are worth a line in a deploy log; neither is worth failing a
    deploy over, which is why this returns findings rather than raising.
    """
    out: list[Finding] = []
    for schema, name, object_type in sorted(objects):
        if schema != SERVING_TIER or object_type != "BASE TABLE":
            continue
        if name in declared:
            continue
        out.append(
            Finding(
                schema,
                name,
                object_type,
                f"undeclared base table in {SERVING_TIER}: gold holds views over "
                "silver plus declared reference data (ADR-128 E1/E3). Declare it "
                f"in {DECLARATION_ENV} if it is authored reference or analysis "
                "data, or derive it from the silver tier.",
            )
        )
    return out


def reads_a_working_tier(views: list[tuple[str, str, str, str]]) -> list[Finding]:
    """Findings over ``(view_schema, view_name, source_schema, source_name)``.

    A gold view reading bronze skips the conform tier entirely: whatever it
    serves was never validated. A gold view reading silver is the normal case
    and the whole point of the tier.
    """
    out: list[Finding] = []
    for view_schema, view_name, source_schema, source_name in sorted(views):
        if view_schema != SERVING_TIER or source_schema != "bronze":
            continue
        out.append(
            Finding(
                view_schema,
                view_name,
                "VIEW",
                f"serves {source_schema}.{source_name} directly: bronze has not "
                "been conformed, so this publishes unvalidated rows. Route it "
                "through silver.",
            )
        )
    return out


def unserved_working_tables(
    objects: list[tuple[str, str, str]],
    views: list[tuple[str, str, str, str]],
) -> list[tuple[str, str]]:
    """Working-tier base tables that no gold object reads.

    Not a violation — most working data is nobody's business but the conform
    pass's. It is the *preventive* half of the rule: this list is exactly the
    set of tables a serving path would have to reach into silver to read. When
    somebody needs one of them served, the answer is a gold view, and knowing
    which tables have no gold surface is how that gets decided before a query
    goes around the rule rather than after.

    Returns ``(schema, name)`` pairs, sorted.
    """
    served = {
        (source_schema, source_name)
        for view_schema, _view_name, source_schema, source_name in views
        if view_schema == SERVING_TIER
    }
    return sorted(
        (schema, name)
        for schema, name, object_type in objects
        if schema in WORKING_TIERS and object_type == "BASE TABLE"
        if (schema, name) not in served
    )


#: Signatures of a TRANSFORM sitting in a gold view definition.
#:
#: Gold derives; it does not transform. The difference is whether the operation
#: changes the shape of an answer or what a record MEANS. ``avg``, ``max``,
#: ``date_trunc`` and window functions are derivation and are not looked for
#: here. Dividing a watt column by a million and calling the result ``_mw`` is
#: a unit conversion, which is silver's first transform.
#:
#: Each is reported for a person to judge, never failed automatically — a rate
#: really is a derivation even though it changes the unit, and no regex can
#: tell that from a conversion. What the regex CAN do is stop it being
#: invisible, which is the whole job: three gold views on one node each
#: converted watts to megawatts independently, and nothing compared them.
VIEW_TRANSFORM_SIGNATURES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "unit conversion",
        re.compile(r"/\s*'?\d{3,}"),
        "dividing by a power of ten renames the unit. The unit vocabulary is "
        "silver's (transform 1, conform); done here, every view that needs the "
        "same conversion makes it again and they can disagree",
    ),
    (
        "unit conversion",
        re.compile(r"\bAS\s+\w+_(mw|kw|gw|kev|mev|ms|us|ns|km|mm)\b", re.IGNORECASE),
        "an output column whose name carries a unit the source column does not "
        "is a conversion; silver's `unit` column is where a unit is declared",
    ),
    (
        "hydration",
        re.compile(r"\bcoalesce\s*\(\s*[\w.\"]+\s*,\s*'"),
        "filling a missing value from a literal is hydration (transform 5), and "
        "a value invented at serving time cannot be told from one that was sent",
    ),
    (
        "repair",
        re.compile(r"\bcase\s+when\b[^;]{0,200}?\bthen\s+'", re.IGNORECASE | re.DOTALL),
        "mapping a value to a literal is a repair (transform 2, clean); the "
        "repaired row should exist in silver, not only in this view",
    ),
    (
        "string cleaning",
        re.compile(r"\b(replace|btrim|trim|lower|upper)\s*\(", re.IGNORECASE),
        "normalising text at serving time is cleaning (transform 2); two views "
        "that clean the same column differently return different answers",
    ),
)

#: Names that say out loud that a view is doing silver's work. Cheap, and it
#: is what caught ``a gold view named `*_clean```.
_CLEANING_NAME = re.compile(r"_(clean|cleaned|fixed|corrected|normalized|normalised)$")


def transforms_in_views(
    definitions: list[tuple[str, str, str]],
) -> list[Finding]:
    """Findings over ``(schema, view_name, definition)``.

    Only gold is examined. Silver views transforming is silver doing its job.
    """
    out: list[Finding] = []
    for schema, name, definition in sorted(definitions):
        if schema != SERVING_TIER:
            continue
        text = definition or ""
        seen: set[str] = set()
        if _CLEANING_NAME.search(name):
            out.append(
                Finding(
                    schema,
                    name,
                    "VIEW",
                    "the name says it cleans. Cleaning is silver's transform 2; "
                    "a gold view derives from records that are already clean",
                )
            )
        for label, pattern, why in VIEW_TRANSFORM_SIGNATURES:
            match = pattern.search(text)
            if not match or label in seen:
                continue
            seen.add(label)
            fragment = " ".join(match.group(0).split())[:60]
            out.append(
                Finding(schema, name, "VIEW", f"{label} in a gold view — {why}. Found: {fragment!r}")
            )
    return out


__all__ = [
    "ALLOWANCES",
    "GOLD_DERIVATIONS",
    "SILVER_TRANSFORMS",
    "Allowance",
    "DECLARATION_ENV",
    "Finding",
    "NOT_ALLOWANCES",
    "SERVING_TIER",
    "TIERS",
    "WORKING_TIERS",
    "classify",
    "declared_gold_tables",
    "VIEW_TRANSFORM_SIGNATURES",
    "reads_a_working_tier",
    "transforms_in_views",
    "unserved_working_tables",
]
