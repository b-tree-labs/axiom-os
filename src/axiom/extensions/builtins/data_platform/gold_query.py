# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Generic medallion answering over the gold tier (ADR-115).

Contract: ``docs/specs/spec-medallion-answering.md``.

This is the executor the base install answers data questions with, so a node
that has ingested *anything* can answer aggregate/series questions about it with
no domain code. Domain packs specialize by declaring named quantities over this
same machinery rather than hand-writing SQL per quantity.

Three properties are structural rather than tested-by-example:

1. **No caller string ever becomes SQL text.** Identifiers are validated against
   the *introspected* schema and then quoted; every literal is a bound
   parameter. The filter grammar is a small closed set of operators, so there is
   no path from caller input to executable SQL.
2. **Unknown objects are typed errors** (:class:`UnknownObject`), raised before a
   statement is built — the caller gets "no such column", not a database error.
3. **Access tiers fail closed.** A table declared restricted whose shape carries
   no tier column refuses to answer rather than answering unfiltered.

Read-only by construction: nothing here emits DDL or DML.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from axiom.uncertainty.serving import companion_join_keys, uncertainty_column_for

#: The medallion's served tier, and the default everything answers from.
GOLD_SCHEMA = "gold"

#: The tiers a caller may name. An allowlist, not a schema argument: "which
#: schema" must never be caller-controlled text, and two tiers are deliberately
#: absent. `bronze` is not tables — it is a filesystem tree, answered by the
#: bronze verbs. `public` is not a medallion tier at all (ADR-052).
#:
#: `silver` is here because an agent asked to explain a gold answer needs to
#: see the conformed rows behind it, and the alternative — raw SQL — has none
#: of this module's guards. Every silver answer says so: see
#: :data:`PRE_SERVED_NOTE`.
ANSWERABLE_TIERS = ("gold", "silver")

#: What a silver answer carries. Gold is what a consumer is meant to read;
#: silver is what conform produced. Today most gold objects are thin views
#: over silver, but that is a fact about this deployment and not a guarantee —
#: a gold view may filter, rename or compute, and a silver table may have no
#: gold projection at all. A reader who does not know which one they got
#: cannot know what was applied.
PRE_SERVED_NOTE = (
    "read from the silver tier: these rows are conformed but not projected, so "
    "anything the gold view filters, renames or computes is not applied here"
)


def resolve_tier(tier: str | None) -> str:
    """Validate a caller-named tier against the allowlist."""
    if tier is None:
        return GOLD_SCHEMA
    name = str(tier).strip().lower()
    if name not in ANSWERABLE_TIERS:
        raise UnknownObject(
            f"{tier!r} is not an answerable tier; expected one of "
            f"{', '.join(ANSWERABLE_TIERS)}"
            + (
                ". bronze is a filesystem tree — use the bronze verbs"
                if name == "bronze"
                else ""
            )
        )
    return name

#: Aggregate name (caller-facing) -> SQL function. Closed set: an unknown name
#: is an error, never interpolated.
_AGG_SQL: dict[str, str] = {
    "sum": "sum",
    "mean": "avg",
    "min": "min",
    "max": "max",
    "std": "stddev_pop",
    "count": "count",
}
AGGREGATE_FNS = frozenset(_AGG_SQL)

#: Comparison operators the filter grammar admits. ``between`` is deliberately
#: absent: ``>=`` + ``<=`` expresses it without an AND-ambiguity in the parser,
#: and a smaller grammar is a smaller attack surface.
_OPS = ("!=", "<=", ">=", "=", "<", ">", "in")

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: A bucket is a Postgres interval literal: "<n> <unit>". Nothing else.
_INTERVAL_RE = re.compile(
    r"^\d+\s+(microsecond|millisecond|second|minute|hour|day|week|month|year)s?$", re.I
)


#: Columns that say what a value IS, as opposed to what it measures. A gold
#: table that carries them is a table where two rows can look alike and mean
#: different things, so an aggregate over it has to say which population it
#: summarised.
#:
#: Split in two, because the consequences differ. Blending these makes a
#: number meaningless rather than merely unqualified:
#:
#: - ``unit`` — a mean over degC and K is not a temperature.
#: - ``source_class`` — a mean over a measurement and a model's prediction is
#:   not a measurement of anything, and it is the failure the source manifest
#:   exists to prevent: an unattributed prediction looks exactly like a
#:   measurement until someone charts the two against each other.
MIXING_HOSTILE = ("unit", "source_class")

#: Blending these changes what the number means without making it nonsense,
#: so they are reported rather than refused:
#:
#: - ``model_ref`` — two models' predictions averaged together is one number
#:   attributable to neither.
#: - ``derivation`` — a ``derived`` channel is computed from others in the
#:   same stream, so a rollup that includes both counts that physics twice.
#: - ``role`` — the cross-site meaning of a channel.
#: - ``quality`` — a flagged sample and a good one are not equal evidence.
MIXING_NOTABLE = ("model_ref", "derivation", "role", "quality")

PROVENANCE_COLUMNS = MIXING_HOSTILE + MIXING_NOTABLE

#: Composition reporting stops here per column. A gold table with thousands of
#: distinct model_refs is a finding of its own, not something to serialise into
#: every answer.
MAX_COMPOSITION_VALUES = 25


class GoldQueryError(ValueError):
    """Base for every caller-facing error from this module."""


class UnknownObject(GoldQueryError):
    """A table, column or aggregate the introspected schema does not have."""


class FilterSyntaxError(GoldQueryError):
    """The filter (or bucket) did not parse under the restricted grammar."""


class AccessTierUnavailable(GoldQueryError):
    """A restricted table cannot be tier-filtered — refuse rather than answer."""


class MixedPopulation(GoldQueryError):
    """The matched rows span values that must not be summarised into one number.

    Raised instead of answering. The message names the column, the values
    found, and the two ways forward — split the answer with ``group_by``, or
    narrow it with ``filter`` — because a caller who gets "refused" and no
    route is a caller who will reach for raw SQL.
    """


@dataclass(frozen=True)
class Clause:
    """One validated ``column <op> value(s)`` predicate."""

    column: str
    op: str
    values: tuple[Any, ...]


# ---------------------------------------------------------------------------
# filter grammar (pure)
# ---------------------------------------------------------------------------


def _parse_literal(raw: str) -> Any:
    """A quoted string or a number. A bare word is rejected on purpose: it would
    otherwise smuggle a column reference (or a keyword) past the grammar."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    raise FilterSyntaxError(
        f"value {raw!r} must be a quoted string or a number (bare identifiers are not allowed)"
    )


def _parse_in_list(raw: str) -> tuple[Any, ...]:
    raw = raw.strip()
    if not (raw.startswith("(") and raw.endswith(")")):
        raise FilterSyntaxError("`in` expects a parenthesised list, e.g. in ('a','b')")
    inner = raw[1:-1].strip()
    if not inner:
        raise FilterSyntaxError("`in` list cannot be empty")
    return tuple(_parse_literal(part) for part in _split_commas(inner))


def _split_commas(text: str) -> list[str]:
    out, buf, in_str = [], [], False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "'":
            in_str = not in_str
            buf.append(ch)
        elif ch == "," and not in_str:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    out.append("".join(buf))
    return [p for p in (s.strip() for s in out) if p]


def _split_and(text: str) -> list[str]:
    """Split on top-level ``AND`` (case-insensitive), respecting quotes."""
    parts, buf, in_str = [], [], False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "'":
            in_str = not in_str
            buf.append(ch)
            i += 1
            continue
        if not in_str and text[i : i + 5].upper() == " AND ":
            parts.append("".join(buf))
            buf = []
            i += 5
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p for p in (s.strip() for s in parts) if p]


def parse_filter(expr: str | None) -> tuple[Clause, ...]:
    """Parse the restricted filter grammar into clauses. Purely syntactic —
    column validity is decided later, against the introspected schema."""
    if not expr or not expr.strip():
        return ()
    clauses: list[Clause] = []
    for part in _split_and(expr):
        for op in _OPS:  # longest-first via _OPS ordering
            token = f" {op} " if op == "in" else op
            idx = part.lower().find(token) if op == "in" else part.find(token)
            if idx <= 0:
                continue
            column = part[:idx].strip()
            rhs = part[idx + len(token) :].strip()
            if not _IDENT_RE.match(column):
                raise FilterSyntaxError(f"{column!r} is not a column name")
            if not rhs:
                raise FilterSyntaxError(f"missing value in {part!r}")
            values = _parse_in_list(rhs) if op == "in" else (_parse_literal(rhs),)
            clauses.append(Clause(column=column, op=op, values=values))
            break
        else:
            raise FilterSyntaxError(
                f"cannot parse {part!r}; expected `<column> <op> <value>` with op in {sorted(_OPS)}"
            )
    return tuple(clauses)


#: Columns whose values name a site. An equality on one of these is
#: expanded to every id that site has ever had — see :func:`_site_values`.
SITE_COLUMNS = frozenset({"site", "site_id"})


def _site_values(values: Sequence[Any]) -> tuple[Any, ...]:
    """*values*, plus every other id the same sites have been known by.

    A site that changed name keeps its history under the old id, and
    rewriting 28.8 million rows to fix that is a 25 GB write on a node with
    57 GB free. Expanding the read is the same answer for no bytes moved:
    a query for the canonical id matches the rows filed under either name,
    so the two are one dataset.

    Order is stable and duplicates are dropped, because the expansion ends
    up in a bound parameter list that a reader may well be diffing.

    Silent when nothing is declared — an id with no aliases expands to
    itself, which is the query the caller wrote.
    """
    try:
        from axiom.infra.site_identity import identities
    except Exception:  # noqa: BLE001 - a read must not need the registry
        return tuple(values)

    registry = identities()
    out: list[Any] = []
    for value in values:
        if not isinstance(value, str):
            out.append(value)
            continue
        canonical = registry.resolve(value)
        for name in registry.aliases_of(canonical) or (canonical,):
            if name not in out:
                out.append(name)
    return tuple(out)


def compile_where(
    clauses: Sequence[Clause], allowed_columns: Iterable[str], placeholder: str = "%s"
) -> tuple[str, list[Any]]:
    """Render clauses to a parameterized WHERE fragment.

    Every identifier is checked against ``allowed_columns`` (the introspected
    set) and then double-quoted; every value becomes a bound parameter.

    An equality or ``in`` on a site column is widened to every id those
    sites have been known by, so a rename does not hide the history it
    renamed. Every other operator is left alone: ``site > 'x'`` is not a
    question about identity, and quietly rewriting it would be worse than
    not answering it.
    """
    allowed = set(allowed_columns)
    sql_parts: list[str] = []
    params: list[Any] = []
    for c in clauses:
        if c.column not in allowed:
            raise UnknownObject(f"no column {c.column!r} (have: {', '.join(sorted(allowed))})")
        ident = '"' + c.column.replace('"', '""') + '"'
        if c.column in SITE_COLUMNS and c.op in ("=", "in"):
            widened = _site_values(c.values)
            if len(widened) > 1:
                marks = ", ".join(placeholder for _ in widened)
                sql_parts.append(f"{ident} IN ({marks})")
                params.extend(widened)
                continue
            # One value: leave the SQL exactly as it was. An `IN` with a
            # single mark is equivalent and needlessly different to read.
            c = Clause(column=c.column, op=c.op, values=widened or c.values)
        if c.op == "in":
            if not c.values:
                # Unreachable via parse_filter (which refuses an empty list),
                # but a rendered ``IN ()`` is invalid SQL, so never build one.
                raise FilterSyntaxError(f"`in` list for {c.column!r} cannot be empty")
            marks = ", ".join(placeholder for _ in c.values)
            sql_parts.append(f"{ident} IN ({marks})")
            params.extend(c.values)
        else:
            sql_parts.append(f"{ident} {c.op} {placeholder}")
            params.append(c.values[0])
    return (" AND ".join(sql_parts), params)


def sql_for_fn(fn: str) -> str:
    """Map a caller-facing aggregate name to its SQL function, or raise."""
    try:
        return _AGG_SQL[fn]
    except KeyError:
        raise UnknownObject(
            f"unknown aggregate {fn!r}; expected one of {sorted(AGGREGATE_FNS)}"
        ) from None


# ---------------------------------------------------------------------------
# access tiers
# ---------------------------------------------------------------------------


def tier_clause(
    columns: Iterable[str],
    access_tiers: Sequence[str],
    *,
    table: str,
    restricted_tables: Iterable[str] = (),
    schema: str = GOLD_SCHEMA,
) -> Clause | None:
    """The tier predicate for ``table``, or ``None`` when tiering does not apply.

    Fail-closed: a table declared restricted that carries no ``access_tier``
    column raises rather than answering unfiltered. A generic verb must never
    widen access relative to the curated verbs it generalizes.
    """
    cols = set(columns)
    if "access_tier" in cols:
        grants = tuple(access_tiers)
        if not grants:
            # An empty grant set renders as ``IN ()`` — a Postgres syntax error,
            # so the deny would surface as a database error instead of a typed
            # one. "You hold no tiers" is also a different answer from "no rows
            # matched", and the caller needs to be able to tell them apart.
            raise AccessTierUnavailable(
                f"no access tiers granted for {schema}.{table}; refusing to "
                "answer rather than answering unfiltered"
            )
        return Clause(column="access_tier", op="in", values=grants)
    if table in set(restricted_tables):
        raise AccessTierUnavailable(
            f"{schema}.{table} is declared restricted but exposes no access_tier "
            "column; refusing to answer rather than answering unfiltered"
        )
    return None


#: The ``basis`` a self test writes, and the only one held out of a served
#: answer by default.
#:
#: **Not a source class.** ``source_class`` names what produced a value, and by
#: that measure a physics run and an onboarding self test are both simulations
#: — so it cannot tell them apart. One is high-fidelity model output somebody
#: asked for, archived as HDF5 and used to train reduced-order models; the
#: other is plumbing exercise nobody asked for. Filtering ``source_class =
#: 'simulated'`` would today catch only self tests, by accident, and would
#: begin hiding the digital twin's own output the moment those runs are
#: ingested under their correct class.
#:
#: ``basis`` answers the question that actually separates them: how the row
#: came to exist. A physics run is ``live``; a self test is ``selftest``.
SELFTEST_BASIS = "selftest"

#: The column that carries it.
BASIS_COLUMN = "basis"

#: What a unit column says when nobody declared one. Mirrors scidisplay so a
#: number never reaches a reader looking dimensionless.
UNIT_NOT_DECLARED = "unit not declared"


def synthetic_clause(
    columns: Iterable[str], *, include_synthetic: bool = False
) -> Clause | None:
    """Hold self-test rows out of a served answer, or ``None`` when the table
    cannot carry them.

    Every new site proves its install by emitting a synthetic signal, and the
    rows are marked, and nothing read the marker: a partner's first chart of
    their own loop would draw the smoke test and the real readings on one
    line, correctly labelled and indistinguishable at a glance.

    Unlike :func:`tier_clause` this does **not** fail closed on a missing
    column, and the difference is deliberate. A missing ``access_tier`` on a
    restricted table is a table that cannot enforce a rule it is subject to. A
    table with no ``source_class`` is a table that holds no signals — a
    catalogue, a configuration — and there is nothing for this to hold out.
    Refusing would make the generic verbs unusable on most of gold.
    """
    if include_synthetic or BASIS_COLUMN not in set(columns):
        return None
    return Clause(column=BASIS_COLUMN, op="!=", values=(SELFTEST_BASIS,))


# ---------------------------------------------------------------------------
# provenance envelope
# ---------------------------------------------------------------------------


def envelope(
    *,
    data: Any,
    source: str,
    method: str,
    rows: int | None = None,
    unit: dict[str, str] | None = None,
    note: str | None = None,
    notes: Sequence[str] | None = None,
    composition: dict[str, dict[str, int]] | None = None,
    attribution: dict[str, Any] | None = None,
    uncertainty: Any | None = None,
) -> dict[str, Any]:
    """The standard result envelope the provenance gate + analytics consume.

    ``attribution`` says what the answer IS where the population agreed —
    unit, source_class, model_ref. ``composition`` says what it was made of
    wherever it did not. Between them a reader can always tell whether they
    are looking at a measurement, a prediction, or a blend someone asked for
    on purpose.

    ``uncertainty`` is an :class:`axiom.uncertainty.serving.Served`, and it
    rides the envelope rather than each verb's ``data`` for the reason the
    rest of the envelope does: there is one of these and there are many
    consumers. Every caller of this function — the CLI, the MCP tools, the
    HTTP routes, the charts, a foreign agent reading the JSON — gets it
    without knowing it was added.

    It is emitted as a dict with every field present, including the nulls.
    A reader must never have to infer absence from a missing key: an
    uncertainty that is absent because nothing reported one, and one that is
    absent because the verb forgot, look identical when the key is simply
    gone. ``claimable: false`` with a ``note`` is a statement; a missing
    ``uncertainty`` key is a silence.
    """
    prov: dict[str, Any] = {"source": source, "method": method}
    if rows is not None:
        prov["rows"] = rows
    combined = ([note] if note else []) + list(notes or [])
    if combined:
        prov["note"] = "; ".join(combined)
    if composition:
        prov["composition"] = composition
    if attribution:
        prov["attribution"] = attribution
    out: dict[str, Any] = {"data": data, "provenance": prov}
    if unit:
        out["unit"] = unit
    if uncertainty is not None:
        out["uncertainty"] = (
            uncertainty.payload() if hasattr(uncertainty, "payload") else uncertainty
        )
    return out


# ---------------------------------------------------------------------------
# uncertainty at the served boundary
# ---------------------------------------------------------------------------


def _uncertainty_projections(fn: str, column: str, unc: str) -> list[str]:
    """The extra SELECT terms that let one pass reconstruct the bound.

    Sufficient statistics, not rows: a window can be millions of rows and
    the point of aggregating in the database is not to bring them back.
    ``FILTER`` scopes every term to rows that actually carried a value, so
    "no value" and "value with no uncertainty" stay distinguishable.
    """
    uq = _quote(unc)
    cq = _quote(column)
    has_value = f"{cq} IS NOT NULL"
    terms = [
        f"count(*) FILTER (WHERE {has_value} AND {uq} IS NOT NULL)",
        f"sum({uq}) FILTER (WHERE {has_value})",
        f"sum({uq} * {uq}) FILTER (WHERE {has_value})",
        f"max({uq}) FILTER (WHERE {has_value})",
    ]
    if fn in ("min", "max"):
        # The uncertainty of the row that WON, not a combination: an
        # extremum is one row's reading.
        order = "ASC" if fn == "min" else "DESC"
        terms.append(f"(array_agg({uq} ORDER BY {cq} {order} NULLS LAST))[1]")
    return terms


def _companion_of(
    cur, table: str, base_columns: set[str], schema: str = GOLD_SCHEMA
) -> tuple[str, tuple[str, ...]] | None:
    """Find ``table``'s structured-uncertainty companion, or nothing.

    Discovery, not configuration: the companion is ``<table>_uncertainty`` in
    the same served schema, and its join keys are its own columns minus the
    term columns. A future conformed shape gets a companion by following the
    naming rather than by an entry in a registry somebody has to remember.

    Every join key must exist on the base table too. A companion whose keys
    the base view does not expose is unreachable, and returning it would
    produce SQL that fails at query time rather than a clean fall back to the
    scalar bound.
    """
    name = f"{table}_uncertainty"
    try:
        companion_columns = {n for n, _ in _columns_of(cur, name, schema)}
    except Exception:  # noqa: BLE001 — absence is the common case, not an error
        return None
    if not companion_columns:
        return None
    keys = companion_join_keys(companion_columns)
    if not keys or not set(keys) <= base_columns:
        return None
    return name, keys


def _structured_terms(
    cur,
    *,
    table: str,
    companion: str,
    keys: tuple[str, ...],
    column: str,
    unc: str | None,
    scale: float,
    wheres: Sequence[str],
    params: Sequence[Any],
    schema: str = GOLD_SCHEMA,
) -> tuple[dict[str, float], int, dict[str, Any]]:
    """Compose the declared sources exactly, in the database.

    Returns ``(effective coefficients, rows carrying structure, loose split)``.

    The arithmetic that matters is one ``GROUP BY``: per symbol, the SUM of
    coefficients and the SUM OF SQUARES. Which one applies is the
    ``independent`` flag — shared sources add, per-reading sources add in
    quadrature — and :func:`axiom.uncertainty.serving.effective_coefficient`
    is the single place that decides.

    The loose split is what prevents a double count. A row that declared
    structure must NOT also contribute its scalar, because the scalar
    summarises the same sources. ``EXISTS`` against the companion makes
    "structured", "magnitude only" and "said nothing" three disjoint counts
    over one window rather than an overlapping guess.

    The window is carried into a DERIVED TABLE rather than onto a join.
    ``wheres`` are built with bare quoted identifiers, and a companion shares
    key column names with its base — ``channel`` is on both — so filtering
    across the join would be ambiguous SQL. Scoping the window first makes
    every reference unambiguous and keeps the two passes over exactly the same
    rows.
    """
    from axiom.uncertainty.serving import effective_coefficient

    comp = f"{_quote(schema)}.{_quote(companion)}"
    where_sql = f" WHERE {' AND '.join(wheres)}" if wheres else ""
    scoped = f"(SELECT * FROM {_quote(schema)}.{_quote(table)}{where_sql})"

    def _on(alias: str) -> str:
        return " AND ".join(f"c.{_quote(k)} = {alias}.{_quote(k)}" for k in keys)

    on = _on("s")
    # Correlated against the INNER table, not the derived table's own alias.
    # Inside `(SELECT *, EXISTS (...) FROM base b WHERE ...) s` the name `s`
    # is not yet bound, so referencing it there is "missing FROM-clause entry"
    # — invalid SQL that a fake cursor accepts happily and Postgres does not.
    exists = f"EXISTS (SELECT 1 FROM {comp} c WHERE {_on('b')})"

    cur.execute(
        f"SELECT c.symbol, c.independent, "
        f"sum(c.coefficient), sum(c.coefficient * c.coefficient) "
        f"FROM {scoped} s JOIN {comp} c ON {on} "
        f"GROUP BY 1, 2",
        list(params),
    )
    term_rows = list(cur.fetchall() or [])

    # One pass for everything scalar: how many rows carry structure (so a mean
    # scales by the right n), and the magnitude-only and silent counts.
    cq = f"s.{_quote(column)}"
    bare = f"NOT s.has_terms AND {cq} IS NOT NULL"
    if unc:
        uq = f"s.{_quote(unc)}"
        projections = [
            "count(*) FILTER (WHERE s.has_terms)",
            f"count(*) FILTER (WHERE {bare} AND {uq} IS NOT NULL)",
            f"sum({uq}) FILTER (WHERE {bare})",
            f"sum({uq} * {uq}) FILTER (WHERE {bare})",
            f"max({uq}) FILTER (WHERE {bare})",
            f"count(*) FILTER (WHERE {bare} AND {uq} IS NULL)",
        ]
    else:
        # No scalar column at all: a row either declared structure or reported
        # nothing. There is no middle kind to count.
        projections = [
            "count(*) FILTER (WHERE s.has_terms)",
            "0",
            "0",
            "0",
            "0",
            f"count(*) FILTER (WHERE {bare})",
        ]
    cur.execute(
        f"SELECT {', '.join(projections)} FROM "
        f"(SELECT b.*, {exists} AS has_terms FROM "
        f"{_quote(schema)}.{_quote(table)} b{where_sql}) s",
        list(params),
    )
    r = cur.fetchone() or (0, 0, None, None, None, 0)
    structured_rows = int(r[0] or 0)
    loose = {
        "loose_quantified": int(r[1] or 0),
        "loose_sum_u": float(r[2] or 0.0),
        "loose_sum_sq": float(r[3] or 0.0),
        "loose_max_u": float(r[4] or 0.0),
        "unquantified": int(r[5] or 0),
    }

    terms: dict[str, float] = {}
    for symbol, independent, sum_a, sum_sq in term_rows:
        terms[str(symbol)] = effective_coefficient(
            sum_a=float(sum_a or 0.0),
            sum_sq=float(sum_sq or 0.0),
            independent=bool(independent),
            scale=scale,
        )
    return terms, structured_rows, loose


def _served_uncertainty(
    *,
    fn: str,
    value: Any,
    n: int,
    stats: Sequence[Any],
    rivals: int | None = None,
) -> Any:
    """Turn one query's uncertainty statistics into a served bound."""
    from axiom.uncertainty import serving

    quantified = int(stats[0] or 0)
    sum_u = float(stats[1] or 0.0)
    sum_sq = float(stats[2] or 0.0)
    max_u = float(stats[3] or 0.0)
    unquantified = max(0, n - quantified)

    if fn == "count":
        return serving.for_count(quantified=quantified, unquantified=unquantified)
    if fn == "mean":
        return serving.for_mean(
            quantified=quantified,
            sum_u=sum_u,
            sum_sq=sum_sq,
            max_u=max_u,
            unquantified=unquantified,
        )
    if fn == "sum":
        return serving.for_sum(
            quantified=quantified,
            sum_u=sum_u,
            sum_sq=sum_sq,
            max_u=max_u,
            unquantified=unquantified,
        )
    if fn == "std":
        mean_u = (sum_u / quantified) if quantified else None
        observed = float(value) if value is not None else None
        return serving.for_dispersion(
            observed=observed,
            mean_u=mean_u,
            quantified=quantified,
            unquantified=unquantified,
        )
    if fn in ("min", "max"):
        selected = stats[4] if len(stats) > 4 else None
        return serving.for_extremum(
            fn=fn,
            selected_u=float(selected) if selected is not None else None,
            rivals=rivals,
            quantified=quantified,
            unquantified=unquantified,
        )
    # Unreachable: `fn` passed sql_for_fn, which admits a closed set. An
    # aggregate added there without a rule here must not silently serve a
    # bare number, so say so rather than returning None.
    return serving.Served(
        fn=fn,
        quantified=quantified,
        unquantified=unquantified,
        note=f"no uncertainty rule is defined for {fn!r}, so none is claimed",
    )


def _extremum_rivals(
    cur,
    *,
    table: str,
    column: str,
    fn: str,
    value: Any,
    selected_u: Any,
    wheres: Sequence[str],
    params: Sequence[Any],
    schema: str = GOLD_SCHEMA,
) -> int | None:
    """How many other rows lie within the extremum's own interval.

    A second pass over the SAME window — the identical WHERE and params —
    so it is one answer to one question, not two answers that could differ.

    This is the finding nothing reported before and the one that looks
    least like a problem: a served peak with forty samples inside its own
    error bar is not the location of a peak, and a threshold check reading
    it as one is acting on noise.
    """
    if value is None or selected_u is None:
        return None
    cq = _quote(column)
    op = "<=" if fn == "min" else ">="
    edge = float(value) + (float(selected_u) if fn == "min" else -float(selected_u))
    where_sql = f" WHERE {' AND '.join(wheres)}" if wheres else ""
    joiner = " AND " if wheres else " WHERE "
    cur.execute(
        f"SELECT count(*) FROM {_quote(schema)}.{_quote(table)}"
        f"{where_sql}{joiner}{cq} {op} %s",
        [*params, edge],
    )
    row = cur.fetchone() or (0,)
    # Minus the winning row itself.
    return max(0, int(row[0] or 0) - 1)


# ---------------------------------------------------------------------------
# introspection + execution (cursor-injected, so it is testable without a DB)
# ---------------------------------------------------------------------------

_COLUMNS_SQL = (
    "SELECT column_name, data_type FROM information_schema.columns "
    "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position"
)
_TABLES_SQL = (
    "SELECT table_name FROM information_schema.tables WHERE table_schema = %s ORDER BY table_name"
)


def _quote(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _columns_of(cur, table: str, schema: str = GOLD_SCHEMA) -> list[tuple[str, str]]:
    if not _IDENT_RE.match(table or ""):
        raise UnknownObject(f"{table!r} is not a table name")
    cur.execute(_COLUMNS_SQL, [schema, table])
    rows = list(cur.fetchall() or [])
    if not rows:
        raise UnknownObject(f"no table {schema}.{table}")
    return [(r[0], r[1]) for r in rows]


def list_tables(cur, tier: str | None = None) -> dict[str, Any]:
    """Every table/view in one answerable tier."""
    schema = resolve_tier(tier)
    cur.execute(_TABLES_SQL, [schema])
    tables = [{"table": r[0]} for r in (cur.fetchall() or [])]
    return envelope(
        data={"schema": schema, "tier": schema, "tables": tables},
        source=schema,
        method="information_schema.tables",
        rows=len(tables),
        notes=[PRE_SERVED_NOTE] if schema != GOLD_SCHEMA else None,
    )


def describe(cur, table: str, tier: str | None = None) -> dict[str, Any]:
    """Columns + types for one table in an answerable tier."""
    schema = resolve_tier(tier)
    cols = _columns_of(cur, table, schema)
    return envelope(
        data={
            "table": table,
            "tier": schema,
            "columns": [{"column": name, "type": dtype} for name, dtype in cols],
        },
        source=f"{schema}.{table}",
        method="information_schema.columns",
        rows=len(cols),
        notes=[PRE_SERVED_NOTE] if schema != GOLD_SCHEMA else None,
    )


def provenance_columns(names: Iterable[str]) -> tuple[str, ...]:
    """The provenance columns this table actually has, in declared order."""
    have = set(names)
    return tuple(c for c in PROVENANCE_COLUMNS if c in have)


def composition(
    cur,
    *,
    table: str,
    columns: Sequence[str],
    where_sql: str,
    params: Sequence[Any],
    schema: str = GOLD_SCHEMA,
) -> dict[str, dict[str, int]]:
    """``{column: {value: row count}}`` over the matched rows.

    One grouped query per provenance column rather than one cross-product:
    the cross-product of five columns is what the caller would have to read,
    and the marginal counts are what tells them whether the population is
    mixed. NULL is reported as a value of its own — "unit unstated" is a
    distinct population from "unit degC", and collapsing them is how a
    unitless value gets served as though it carried one.
    """
    out: dict[str, dict[str, int]] = {}
    for col in columns:
        sql = (
            f"SELECT {_quote(col)}, count(*) "
            f"FROM {_quote(schema)}.{_quote(table)}{where_sql} "
            f"GROUP BY 1 ORDER BY 2 DESC LIMIT {MAX_COMPOSITION_VALUES + 1}"
        )
        cur.execute(sql, list(params))
        rows = list(cur.fetchall() or [])
        if not rows:
            continue
        out[col] = {("" if r[0] is None else str(r[0])): int(r[1] or 0) for r in rows}
    return out


def _mixing_note(col: str, values: dict[str, int]) -> str:
    rendered = ", ".join(
        f"{name or 'unstated'} ({count:,})" for name, count in list(values.items())[:6]
    )
    return f"{col} spans {len(values)} values: {rendered}"


def guard_population(
    comp: dict[str, dict[str, int]],
    *,
    group_by: Sequence[str],
    allow_mixed: bool,
    fn: str,
    allow_mixed_units: bool = False,
) -> list[str]:
    """Refuse a blended answer, or return the notes a caller must be told.

    ``group_by`` resolves a mix rather than suppressing it: the answer is split
    along that column, so each number summarises one population. ``count`` is
    exempt from refusal because counting rows across units or source classes is
    a real count of rows — it is not a quantity that the mixing corrupts.

    ``allow_mixed_units`` waives the unit check alone, for a caller who knows
    the rows share a scale despite the labels. It does not excuse a blend of
    source classes: a shared scale says nothing about whether a number was
    measured or predicted.
    """
    grouped = set(group_by)
    notes: list[str] = []
    for col, values in comp.items():
        if len(values) < 2 or col in grouped:
            continue
        waived = allow_mixed or (col == UNIT_COLUMN and allow_mixed_units)
        if col in MIXING_HOSTILE and fn != "count" and not waived:
            why = ""
            if col == UNIT_COLUMN:
                _, verdict = _unit_verdict(list(values), allow_mixed=False)
                why = f"{verdict} " if verdict else ""
            raise MixedPopulation(
                f"refusing to summarise across {len(values)} {col} values — "
                f"{_mixing_note(col, values)}. {why}"
                f"Split it with group_by={col!r}, narrow it with a filter, "
                f"or pass allow_mixed=true to state that the blend is intended"
                + (
                    " (allow_mixed_units=true if only the unit labels differ "
                    "and the rows share a scale)."
                    if col == UNIT_COLUMN and "allow_mixed_units" not in why
                    else "."
                )
            )
        notes.append(_mixing_note(col, values))
    return notes


def _answer_unit(comp: dict[str, dict[str, int]], groups: Sequence[str]) -> str | None:
    """The one unit an answer is in, or ``None`` when it is not in one.

    Read from the same composition the guard judged, so the unit reported and
    the population refused or allowed are the same row set. Grouping by unit
    leaves no single unit to report: each group carries its own.
    """
    if UNIT_COLUMN in groups or UNIT_COLUMN not in comp:
        return None
    unit, _ = _unit_verdict(list(comp[UNIT_COLUMN]), allow_mixed=True)
    return unit


def attribution(comp: dict[str, dict[str, int]]) -> dict[str, Any]:
    """What this answer is, for the provenance columns that resolved to one value.

    A single-valued provenance column is a claim the answer can carry: this
    number is degC, it is predicted, it came out of that model. A multi-valued
    one is deliberately absent here — it appears in ``composition`` instead,
    because summarising it away is the thing this module refuses to do.
    """
    return {
        col: (None if next(iter(values)) == "" else next(iter(values)))
        for col, values in comp.items()
        if len(values) == 1
    }


def _validated_group_by(group_by: Sequence[str] | None, names: set[str]) -> tuple[str, ...]:
    out: list[str] = []
    for col in group_by or ():
        col = str(col)
        if col not in names:
            raise UnknownObject(f"no column {col!r} to group by")
        if col not in out:
            out.append(col)
    return tuple(out)


def _window_and_filter(
    names: set[str],
    *,
    window: dict[str, Any] | None,
    filter: str | None,
    access_tiers: Sequence[str],
    table: str,
    restricted_tables: Iterable[str],
    schema: str = GOLD_SCHEMA,
    include_synthetic: bool = False,
) -> tuple[list[str], list[Any], list[str]]:
    """Build the shared WHERE pieces: caller filter, window, tier, synthetic."""
    wheres: list[str] = []
    params: list[Any] = []
    described: list[str] = []

    clauses = list(parse_filter(filter))
    tier = tier_clause(
        names, access_tiers, table=table,
        restricted_tables=restricted_tables, schema=schema,
    )
    if tier is not None:
        clauses.append(tier)
    synthetic = synthetic_clause(names, include_synthetic=include_synthetic)
    if synthetic is not None:
        clauses.append(synthetic)
    if clauses:
        sql, ps = compile_where(clauses, names)
        wheres.append(sql)
        params.extend(ps)
        described.append(filter or "")
        if tier is not None:
            # The tier filter changes which rows the number came from, so it
            # belongs in the provenance: two callers with different grants
            # otherwise get different answers carrying identical method strings.
            described.append(f"access_tier in {list(tier.values)}")
        if synthetic is not None:
            # Same reasoning, and more load-bearing: an answer that quietly
            # dropped the install's own smoke test looks exactly like one that
            # never met it.
            described.append(f"{BASIS_COLUMN} != {SELFTEST_BASIS!r}")

    if window:
        wcol = window.get("column")
        if wcol not in names:
            raise UnknownObject(f"no column {wcol!r} for the window")
        start, end = window.get("start"), window.get("end")
        if start is not None:
            wheres.append(f"{_quote(wcol)} >= %s")
            params.append(start)
        if end is not None:
            wheres.append(f"{_quote(wcol)} < %s")
            params.append(end)
        described.append(f"window {start}..{end} on {wcol}")

    return wheres, params, [d for d in described if d]


#: The column an answer's units come from.
UNIT_COLUMN = "unit"


def _unit_verdict(
    units: list[str], *, allow_mixed: bool
) -> tuple[str | None, str | None]:
    """``(unit_to_report, refusal)`` for the units an answer actually spanned.

    A single-site aggregate over the signals table was measured spanning
    **eight** units at once — percent, degC, L/min, psi, %RH, seconds and
    sccm, plus rows declaring none — and it returned a number. Averaging
    seconds with degrees Celsius produces a value with no meaning, and
    presenting it as a result is the failure this whole week has been about:
    it is not a wrong number, it is a number that was never a quantity.

    A refusal is the right answer because there is no correct value to return.
    ``allow_mixed`` exists for a caller who knows the rows share a scale
    despite the labels, and it does not change the arithmetic — it only stops
    the refusal.
    """
    stated = sorted(u for u in units if u)
    undeclared = any(not u for u in units)

    if len(stated) > 1 and not allow_mixed:
        return None, (
            f"this answer spans {len(stated)} units ({', '.join(stated)})"
            + (" and rows declaring none" if undeclared else "")
            + ". Aggregating across them produces a number that was never a "
            "quantity, so none is returned. Narrow the filter, or pass "
            "allow_mixed_units if the rows share a scale despite the labels."
        )
    if stated and undeclared and not allow_mixed:
        return None, (
            f"this answer mixes rows in {stated[0] if len(stated) == 1 else ', '.join(stated)} "
            "with rows declaring no unit. An undeclared unit is not a matching "
            "one, so none is returned."
        )
    if len(stated) == 1:
        return stated[0], None
    if not stated and undeclared:
        return UNIT_NOT_DECLARED, None
    return None, None


def aggregate(
    cur,
    *,
    table: str,
    column: str,
    fn: str,
    window: dict[str, Any] | None = None,
    filter: str | None = None,
    group_by: Sequence[str] | None = None,
    allow_mixed: bool = False,
    tier: str | None = None,
    access_tiers: Sequence[str] = ("public",),
    restricted_tables: Iterable[str] = (),
    include_synthetic: bool = False,
    allow_mixed_units: bool = False,
) -> dict[str, Any]:
    """One deterministic aggregate of ``column`` over ``table``.

    Empty window → ``data: null`` with a provenance note. Never a fabricated 0
    (``count`` of nothing is still a real 0 and is reported as such).

    If the matched rows span more than one ``unit`` or ``source_class``, this
    refuses rather than returning a blended number. ``group_by`` is the way
    through: it splits the answer so each value summarises one population,
    which is also how a measured-versus-predicted comparison is expressed.
    """
    schema = resolve_tier(tier)
    cols = _columns_of(cur, table, schema)
    names = {n for n, _ in cols}
    if column not in names:
        raise UnknownObject(f"no column {column!r} on {schema}.{table}")
    agg = sql_for_fn(fn)
    groups = _validated_group_by(group_by, names)

    wheres, params, described = _window_and_filter(
        names, window=window, filter=filter, access_tiers=access_tiers,
        table=table, restricted_tables=restricted_tables, schema=schema,
        include_synthetic=include_synthetic,
    )
    where_sql = f" WHERE {' AND '.join(wheres)}" if wheres else ""

    prov_cols = provenance_columns(names)
    comp = (
        composition(
            cur, table=table, columns=prov_cols,
            where_sql=where_sql, params=params, schema=schema,
        )
        if prov_cols
        else {}
    )
    notes = guard_population(
        comp, group_by=groups, allow_mixed=allow_mixed, fn=fn,
        allow_mixed_units=allow_mixed_units,
    )
    if schema != GOLD_SCHEMA:
        notes.insert(0, PRE_SERVED_NOTE)
    if allow_mixed and any(len(v) > 1 for k, v in comp.items() if k in MIXING_HOSTILE):
        notes.append("allow_mixed was set: this number blends populations on purpose")
    answer_unit = _answer_unit(comp, groups)

    source = f"{schema}.{table}"
    method = f"{agg}({column})"
    if groups:
        method += f" by {', '.join(groups)}"
    if described:
        method += "; " + "; ".join(described)

    if groups:
        select = ", ".join(_quote(g) for g in groups)
        sql = (
            f"SELECT {select}, {agg}({_quote(column)}), count({_quote(column)}), count(*) "
            f"FROM {_quote(schema)}.{_quote(table)}{where_sql} "
            f"GROUP BY {select} ORDER BY {select}"
        )
        cur.execute(sql, params)
        rows = list(cur.fetchall() or [])
        k = len(groups)
        groups_out = [
            dict(zip(groups, r[:k], strict=False))
            | {"value": r[k], "rows": int(r[k + 1] or 0), "matched": int(r[k + 2] or 0)}
            for r in rows
        ]
        if not groups_out:
            return envelope(
                data=None, source=source, method=method, rows=0,
                note="no rows matched; no value to report",
                notes=notes, composition=comp,
            )
        return envelope(
            data={
                "table": table, "column": column, "fn": fn,
                "group_by": list(groups), "groups": groups_out,
            },
            source=source, method=method, rows=sum(g["rows"] for g in groups_out),
            notes=notes, composition=comp, attribution=attribution(comp),
            unit={"value": answer_unit} if answer_unit else None,
        )

    # count(col) counts VALUES; count(*) counts ROWS. Without both, "the window
    # was empty" and "every row had a null here" are indistinguishable, and they
    # call for opposite responses from the caller.
    unc = uncertainty_column_for(column, names)
    projections = [f"{agg}({_quote(column)})", f"count({_quote(column)})", "count(*)"]
    if unc:
        projections.extend(_uncertainty_projections(fn, column, unc))
    sql = f"SELECT {', '.join(projections)} FROM {_quote(schema)}.{_quote(table)}{where_sql}"
    cur.execute(sql, params)
    row = cur.fetchone() or (None, 0, 0)
    value, n = row[0], int(row[1] or 0)
    matched = int(row[2] or 0) if len(row) > 2 else n

    if value is None and fn != "count":
        note = (
            "no rows matched; no value to report"
            if matched == 0
            else f"{matched} row(s) matched but {column!r} is null in all of them; "
            "no value to report"
        )
        return envelope(
            data=None, source=source, method=method, rows=n, note=note,
            notes=notes, composition=comp,
        )

    served = None
    # Structure first. A companion table carries the SOURCES rather than a
    # magnitude, so correlation is computed from shared symbols instead of
    # assumed — which collapses the bound to an exact figure. That collapse is
    # the payoff of declaring provenance: declaring more makes the answer
    # NARROWER, so the incentive points the right way.
    #
    # Only for the aggregates where composing sources is meaningful. An
    # extremum is one row's reading and a dispersion is not a combination at
    # all, so those keep the scalar rules regardless.
    companion = _companion_of(cur, table, names, schema) if fn in ("mean", "sum") else None
    if companion is not None:
        from axiom.uncertainty.serving import combine_structured

        companion_name, keys = companion
        # 1/n over every row the aggregate averaged, which is what the served
        # VALUE was divided by. Scaling by the declared subset instead would
        # report the uncertainty of a different quantity.
        scale = (1.0 / n) if (fn == "mean" and n) else 1.0
        terms, structured_rows, loose = _structured_terms(
            cur,
            table=table,
            companion=companion_name,
            keys=keys,
            column=column,
            unc=unc,
            scale=scale,
            wheres=wheres,
            params=params,
            schema=schema,
        )
        if terms or loose["loose_quantified"] or loose["unquantified"]:
            served = combine_structured(
                terms=terms, structured_rows=structured_rows, scale=scale, fn=fn, **loose
            )
    if served is None and unc:
        stats = list(row[3:])
        rivals = None
        if fn in ("min", "max") and len(stats) > 4:
            rivals = _extremum_rivals(
                cur,
                table=table,
                column=column,
                fn=fn,
                value=value,
                selected_u=stats[4],
                wheres=wheres,
                params=params,
                schema=schema,
            )
        served = _served_uncertainty(fn=fn, value=value, n=n, stats=stats, rivals=rivals)

    return envelope(
        data={"table": table, "column": column, "fn": fn, "value": value},
        source=source,
        method=method,
        rows=n,
        notes=notes,
        composition=comp,
        attribution=attribution(comp),
        uncertainty=served,
        unit={"value": answer_unit} if answer_unit else None,
    )


#: The signals table. Roles live on signals and nowhere else, so the role
#: verbs do not take a table: there is one, and inventing a parameter for it
#: would invite a caller to point them at something with no roles and get an
#: empty answer that looks like "no data" rather than "wrong question".
SIGNALS_TABLE = "signals"


def roles(cur, *, include_synthetic: bool = False) -> dict[str, Any]:
    """Which quantities the fleet can answer, and which sites answer each.

    The catalogue's other verbs are keyed on table and column, and nobody asks
    a question in those terms. A researcher asks about a quantity — wall
    a temperature, a position, a power — and the only part of a signal that
    carries a quantity portably is its role. A channel name does not: one site
    was found carrying ``XR``, ``long_name_for_xr`` and ``Xr`` for one instrument.

    So this answers what the fleet can be asked, rather than requiring
    somebody to hold four sites' naming schemes in their head first.

    Reads the partial index on ``(role, ts) WHERE role IS NOT NULL``, which is
    why it does not need a window to be affordable.
    """
    cols = {n for n, _ in _columns_of(cur, SIGNALS_TABLE)}
    for needed in ("role", "site", "unit"):
        if needed not in cols:
            raise UnknownObject(
                f"no column {needed!r} on {GOLD_SCHEMA}.{SIGNALS_TABLE}; this "
                "build cannot answer role questions"
            )
    wheres = ["role IS NOT NULL"]
    params: list[Any] = []
    synthetic = synthetic_clause(cols, include_synthetic=include_synthetic)
    if synthetic is not None:
        sql_frag, ps = compile_where([synthetic], cols)
        wheres.append(sql_frag)
        params.extend(ps)

    cur.execute(
        "SELECT role, site, unit, count(*) "
        f"FROM {_quote(GOLD_SCHEMA)}.{_quote(SIGNALS_TABLE)} "
        f"WHERE {' AND '.join(wheres)} "
        "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3",
        params,
    )
    by_role: dict[str, dict[str, Any]] = {}
    for role, site, unit, n in cur.fetchall() or []:
        entry = by_role.setdefault(
            str(role), {"role": str(role), "sites": {}, "units": set(), "rows": 0}
        )
        entry["sites"].setdefault(str(site), 0)
        entry["sites"][str(site)] += int(n or 0)
        entry["rows"] += int(n or 0)
        entry["units"].add(str(unit or ""))

    out = []
    for entry in by_role.values():
        units = sorted(u for u in entry["units"] if u)
        undeclared = "" in entry["units"]
        out.append({
            "role": entry["role"],
            "sites": sorted(entry["sites"]),
            "rows": entry["rows"],
            "units": units,
            # A role answered in two units is not one question with two
            # answers; it is two questions sharing a name. Said here so a
            # comparison does not have to discover it.
            "comparable": len(entry["sites"]) > 1 and len(units) == 1 and not undeclared,
            "note": _role_note(entry["sites"], units, undeclared),
        })
    return envelope(
        data={"roles": out},
        source=f"{GOLD_SCHEMA}.{SIGNALS_TABLE}",
        method="group by role, site, unit",
        rows=sum(r["rows"] for r in out),
    )


def _role_note(sites: dict[str, int], units: list[str], undeclared: bool) -> str:
    if len(sites) < 2:
        return "only one site answers this, so there is nothing to compare it with yet"
    if undeclared and units:
        return (
            f"some rows carry no unit and others carry {', '.join(units)}; a "
            "comparison would be mixing a stated unit with an unstated one"
        )
    if undeclared:
        return "no site declares a unit for this, so the values cannot be compared"
    if len(units) > 1:
        return (
            f"answered in {len(units)} different units ({', '.join(units)}); "
            "these are two questions sharing a name rather than one question "
            "with two answers"
        )
    return f"{len(sites)} sites, all in {units[0]}"


def compare(
    cur,
    *,
    role: str,
    bucket: str,
    sites: Sequence[str] = (),
    fn: str = "mean",
    window: dict[str, Any] | None = None,
    tiers: Sequence[str] = ("public",),
    include_synthetic: bool = False,
    allow_mixed_units: bool = False,
) -> dict[str, Any]:
    """One quantity across sites, bucketed onto a shared time axis.

    The verb a cross-site question actually wants, and the one that makes
    declaring a role worth the effort.

    **It refuses to overlay two units.** A figure drawing kW against degC on
    one axis is a lie about comparability, and an aggregate over both is worse
    because the lie has no shape to notice. Where the sites disagree this
    returns each unit's series separately rather than one merged answer, and
    ``allow_mixed_units`` does not merge them either — it only stops the
    refusal, because there is no correct way to merge them and a flag should
    not pretend otherwise.
    """
    if not _INTERVAL_RE.match((bucket or "").strip()):
        raise FilterSyntaxError(
            f"bucket {bucket!r} must be an interval like '1 hour' or '15 minutes'"
        )
    cols = {n for n, _ in _columns_of(cur, SIGNALS_TABLE)}
    agg = sql_for_fn(fn)

    clauses = [Clause(column="role", op="=", values=(str(role),))]
    if sites:
        clauses.append(Clause(column="site", op="in", values=tuple(str(s) for s in sites)))
    tier = tier_clause(cols, tiers, table=SIGNALS_TABLE)
    if tier is not None:
        clauses.append(tier)
    synthetic = synthetic_clause(cols, include_synthetic=include_synthetic)
    if synthetic is not None:
        clauses.append(synthetic)
    where_sql, params = compile_where(clauses, cols)

    described = [f"role = {role!r}"]
    if sites:
        described.append(f"site in {sorted(sites)}")
    if synthetic is not None:
        described.append(f"{BASIS_COLUMN} != {SELFTEST_BASIS!r}")

    if window:
        wcol = str(window.get("column") or "ts")
        if wcol not in cols:
            raise UnknownObject(f"no column {wcol!r} on {GOLD_SCHEMA}.{SIGNALS_TABLE}")
        for bound, op in (("from", ">="), ("to", "<=")):
            if window.get(bound):
                where_sql += f" AND {_quote(wcol)} {op} %s"
                params.append(window[bound])
        described.append(f"window {window.get('from', '')}..{window.get('to', '')}")

    cur.execute(
        "SELECT site, unit, "
        "date_bin(%s::interval, ts, TIMESTAMP 'epoch') AS bucket, "
        f"{agg}(value) "
        f"FROM {_quote(GOLD_SCHEMA)}.{_quote(SIGNALS_TABLE)} WHERE {where_sql} "
        "GROUP BY 1, 2, 3 ORDER BY 2, 1, 3",
        [bucket, *params],
    )
    rows = list(cur.fetchall() or [])
    by_unit: dict[str, dict[str, list[dict[str, Any]]]] = {}
    total = 0
    for site, unit, moment, value in rows:
        total += 1
        by_unit.setdefault(str(unit or ""), {}).setdefault(str(site), []).append(
            {"t": moment, "value": value}
        )

    method = f"{agg}(value) by {bucket}; " + "; ".join(described)
    source = f"{GOLD_SCHEMA}.{SIGNALS_TABLE}"

    if not by_unit:
        return envelope(
            data=None, source=source, method=method, rows=0,
            note=f"no rows carry role {role!r} in this window",
        )

    undeclared = "" in by_unit
    if len(by_unit) > 1 and not allow_mixed_units:
        stated = sorted(u for u in by_unit if u)
        return envelope(
            data=None, source=source, method=method, rows=total,
            note=(
                f"role {role!r} is answered in "
                + (f"{len(stated)} units ({', '.join(stated)})" if stated else "no unit")
                + (" and by rows carrying no unit" if undeclared and stated else "")
                + ". Overlaying them would be a claim about comparability that "
                "is not true. Pass allow_mixed_units to see each separately."
            ),
        )

    return envelope(
        data={
            "role": role,
            "bucket": bucket,
            "by_unit": {
                unit or UNIT_NOT_DECLARED: {
                    "sites": {site: points for site, points in sorted(sites_points.items())}
                }
                for unit, sites_points in sorted(by_unit.items())
            },
        },
        source=source, method=method, rows=total,
    )


def series(
    cur,
    *,
    table: str,
    column: str,
    bucket: str,
    time_column: str,
    fn: str = "mean",
    window: dict[str, Any] | None = None,
    filter: str | None = None,
    group_by: Sequence[str] | None = None,
    allow_mixed: bool = False,
    tier: str | None = None,
    access_tiers: Sequence[str] = ("public",),
    restricted_tables: Iterable[str] = (),
    include_synthetic: bool = False,
    allow_mixed_units: bool = False,
) -> dict[str, Any]:
    """A bucketed series — the input shape the analytics tool consumes.

    With ``group_by`` the result is one named series per group rather than one
    series, which is what a measured-against-predicted overlay is: the same
    quantity, the same buckets, one line per ``source_class``. Without it, the
    same refusal as :func:`aggregate` applies — a single line blended across
    units or source classes is a line that means nothing.
    """
    if not _INTERVAL_RE.match((bucket or "").strip()):
        raise FilterSyntaxError(
            f"bucket {bucket!r} must be an interval like '1 hour' or '15 minutes'"
        )
    schema = resolve_tier(tier)
    cols = _columns_of(cur, table, schema)
    names = {n for n, _ in cols}
    for needed in (column, time_column):
        if needed not in names:
            raise UnknownObject(f"no column {needed!r} on {schema}.{table}")
    agg = sql_for_fn(fn)
    groups = _validated_group_by(group_by, names)

    wheres, params, described = _window_and_filter(
        names, window=window, filter=filter, access_tiers=access_tiers,
        table=table, restricted_tables=restricted_tables, schema=schema,
        include_synthetic=include_synthetic,
    )
    where_sql = f" WHERE {' AND '.join(wheres)}" if wheres else ""

    # A series is judged across EVERY bucket, not per bucket: the composition
    # spans the whole window. A line whose units change halfway is the worst
    # version of a blend, because each bucket is internally consistent and the
    # line is a lie.
    prov_cols = provenance_columns(names)
    comp = (
        composition(
            cur, table=table, columns=prov_cols,
            where_sql=where_sql, params=params, schema=schema,
        )
        if prov_cols
        else {}
    )
    notes = guard_population(
        comp, group_by=groups, allow_mixed=allow_mixed, fn=fn,
        allow_mixed_units=allow_mixed_units,
    )
    if schema != GOLD_SCHEMA:
        notes.insert(0, PRE_SERVED_NOTE)
    if allow_mixed and any(len(v) > 1 for k, v in comp.items() if k in MIXING_HOSTILE):
        notes.append("allow_mixed was set: these points blend populations on purpose")
    answer_unit = _answer_unit(comp, groups)

    source = f"{schema}.{table}"
    method = f"{agg}({column}) by {bucket}"
    if groups:
        method += f", split on {', '.join(groups)}"
    if described:
        method += "; " + "; ".join(described)

    # The bucket is regex-validated against a closed interval grammar, then
    # bound as a parameter to date_bin's interval argument. GROUP BY is by
    # ordinal so the grouping columns and the bucket are named once.
    # Per bucket, the same sufficient statistics the scalar aggregate uses,
    # so every point in a series carries its own bound rather than the
    # series carrying one figure that fits none of them.
    unc = uncertainty_column_for(column, names)
    k = len(groups)
    projections = [
        *(_quote(g) for g in groups),
        f"date_bin(%s::interval, {_quote(time_column)}, TIMESTAMP 'epoch') AS bucket",
        f"{agg}({_quote(column)})",
        f"count({_quote(column)})",
    ]
    stats_from = len(projections)
    if unc:
        projections.extend(_uncertainty_projections(fn, column, unc))
    ordinals = ", ".join(str(i) for i in range(1, k + 2))
    sql = (
        f"SELECT {', '.join(projections)} "
        f"FROM {_quote(schema)}.{_quote(table)}{where_sql} "
        f"GROUP BY {ordinals} ORDER BY {ordinals}"
    )
    cur.execute(sql, [bucket, *params])
    rows = list(cur.fetchall() or [])

    def _point(r: Sequence[Any]) -> dict[str, Any]:
        point: dict[str, Any] = {"t": r[k], "value": r[k + 1]}
        if unc and len(r) > k + 3:
            # `rivals=None` deliberately: establishing whether a bucket's
            # extremum is distinguishable would be one extra query PER
            # BUCKET, and a silent 0 would read as "unambiguous" — a
            # positive claim nobody made. The note says it was not
            # determined.
            point["uncertainty"] = _served_uncertainty(
                fn=fn, value=r[k + 1], n=int(r[k + 2] or 0), stats=list(r[stats_from:]),
                rivals=None,
            ).payload()
        return point

    if not rows:
        return envelope(
            data=None,
            source=source,
            method=method,
            rows=0,
            note="no rows matched; no series to report",
            notes=notes, composition=comp,
        )

    if groups:
        k = len(groups)
        by_key: dict[tuple, list[dict[str, Any]]] = {}
        for r in rows:
            by_key.setdefault(tuple(r[:k]), []).append(_point(r))
        series_out = [
            {"key": dict(zip(groups, key, strict=False)), "points": points}
            for key, points in by_key.items()
        ]
        return envelope(
            data={
                "table": table, "column": column, "bucket": bucket, "fn": fn,
                "group_by": list(groups), "series": series_out,
            },
            source=source, method=method,
            rows=sum(len(s["points"]) for s in series_out),
            notes=notes, composition=comp, attribution=attribution(comp),
            unit={"value": answer_unit} if answer_unit else None,
        )

    points = [_point(r) for r in rows]
    return envelope(
        data={"table": table, "column": column, "bucket": bucket, "fn": fn, "series": points},
        source=source, method=method, rows=len(points),
        notes=notes, composition=comp, attribution=attribution(comp),
        unit={"value": answer_unit} if answer_unit else None,
    )


__all__ = [
    "AGGREGATE_FNS",
    "ANSWERABLE_TIERS",
    "PRE_SERVED_NOTE",
    "MAX_COMPOSITION_VALUES",
    "MIXING_HOSTILE",
    "MIXING_NOTABLE",
    "PROVENANCE_COLUMNS",
    "AccessTierUnavailable",
    "Clause",
    "FilterSyntaxError",
    "GOLD_SCHEMA",
    "GoldQueryError",
    "MixedPopulation",
    "UnknownObject",
    "aggregate",
    "attribution",
    "compile_where",
    "composition",
    "describe",
    "coverage_verdict",
    "envelope",
    "guard_population",
    "list_tables",
    "parse_filter",
    "provenance_columns",
    "resolve_tier",
    "series",
    "sql_for_fn",
    "tier_clause",
    "uncertainty_coverage",
]


# ---------------------------------------------------------------------------
# is the uncertainty surface actually populated?
# ---------------------------------------------------------------------------
#
# An apparatus with nothing declared behind it serves `claimable: false` on
# every read and looks entirely healthy. That is the channel-map failure one
# level down — a map can be complete and match nothing — so coverage is a
# surface rather than something a reader has to go counting for themselves.


def uncertainty_coverage(
    cur, *, site: str | None = None, include_sources: bool = True
) -> dict[str, Any]:
    """Per-(site, stream) uncertainty coverage, with the source inventory.

    Reports three disjoint populations per stream, which are the three kinds
    of absence from ADR-136 D5 counted over real rows:

    - **structured** — sources declared, so an aggregate is EXACT;
    - **magnitude only** — a scalar, so an aggregate is a BOUND;
    - **silent** — nothing, so an aggregate claims nothing.

    A stream that is entirely silent is the honest verdict on an ingest that
    has not been told what its instruments are worth. It is not a database
    problem and it is not a bug in this package, and it is worth saying out
    loud because every served figure from it will be unclaimable while
    nothing anywhere reports an error.
    """
    wheres: list[str] = []
    params: list[Any] = []
    if site:
        wheres.append("site = %s")
        params.append(site)
    where_sql = f" WHERE {' AND '.join(wheres)}" if wheres else ""

    cur.execute(
        "SELECT site, stream, points, channels, with_magnitude, with_structure, "
        "silent, first_ts, last_ts "
        f"FROM {_quote(GOLD_SCHEMA)}.{_quote('uncertainty_coverage')}{where_sql} "
        "ORDER BY points DESC",
        params,
    )
    streams = [
        {
            "site": r[0],
            "stream": r[1],
            "points": int(r[2] or 0),
            "channels": int(r[3] or 0),
            "structured": int(r[5] or 0),
            # A row carrying structure also carries a summary scalar, and
            # counting it in both populations would make the fractions sum
            # past one. `with_magnitude` includes it; this does not.
            "magnitude_only": max(0, int(r[4] or 0) - int(r[5] or 0)),
            "silent": int(r[6] or 0),
            "first_ts": r[7],
            "last_ts": r[8],
        }
        for r in (cur.fetchall() or [])
    ]
    for s in streams:
        n = s["points"] or 1
        s["structured_fraction"] = s["structured"] / n
        s["magnitude_fraction"] = s["magnitude_only"] / n
        s["silent_fraction"] = s["silent"] / n
        s["exact"] = s["structured"] == s["points"]
        s["unclaimable"] = s["silent"] == s["points"]

    sources: list[dict[str, Any]] = []
    if include_sources:
        cur.execute(
            "SELECT site, stream, symbol, independent, points, channels, "
            "min_coefficient, max_coefficient "
            f"FROM {_quote(GOLD_SCHEMA)}.{_quote('uncertainty_sources')}{where_sql} "
            "ORDER BY points DESC, symbol",
            params,
        )
        sources = [
            {
                "site": r[0],
                "stream": r[1],
                "symbol": r[2],
                # Surfaced prominently because it is the field that decides
                # whether an error averages away. A source declared shared
                # when it is per-reading gives a bound too wide; declared
                # per-reading when it is shared gives a figure too confident
                # by sqrt(n), and nothing downstream can tell.
                "independent": bool(r[3]),
                "points": int(r[4] or 0),
                "channels": int(r[5] or 0),
                "min_coefficient": float(r[6]) if r[6] is not None else None,
                "max_coefficient": float(r[7]) if r[7] is not None else None,
            }
            for r in (cur.fetchall() or [])
        ]

    totals = {
        "points": sum(s["points"] for s in streams),
        "structured": sum(s["structured"] for s in streams),
        "magnitude_only": sum(s["magnitude_only"] for s in streams),
        "silent": sum(s["silent"] for s in streams),
        "streams": len(streams),
        "sources": len({(s["site"], s["symbol"]) for s in sources}),
    }
    return envelope(
        data={"totals": totals, "streams": streams, "sources": sources},
        source=f"{GOLD_SCHEMA}.uncertainty_coverage",
        method="coverage by (site, stream)" + (f"; site = {site}" if site else ""),
        rows=len(streams),
        note=coverage_verdict(totals, streams),
    )


def coverage_verdict(totals: dict[str, Any], streams: list[dict[str, Any]]) -> str:
    """One sentence a person can act on, or the reason there is nothing to say.

    Deliberately blunt about the case that matters: an apparatus with no
    declarations is not partially working, it is serving nothing, and a
    percentage dressed up as progress would obscure that.
    """
    n = totals["points"]
    if n == 0:
        return "no conformed rows, so there is nothing to have an uncertainty about"
    if totals["silent"] == n:
        return (
            f"none of {n} served point(s) carries an uncertainty, so every "
            "aggregate over them reports none. Nothing is broken and nothing "
            "will report an error — no ingest has been told what its "
            "instruments are worth"
        )
    if totals["structured"] == n:
        return (
            f"every one of {n} served point(s) declares its sources, so "
            "aggregates over them are exact rather than bounded"
        )
    parts = [
        f"{totals['structured']} exact",
        f"{totals['magnitude_only']} bounded",
        f"{totals['silent']} unclaimable",
    ]
    worst = [s for s in streams if s["unclaimable"]]
    tail = ""
    if worst:
        named = ", ".join(f"{s['site']}/{s['stream']}" for s in worst[:3])
        more = f" and {len(worst) - 3} more" if len(worst) > 3 else ""
        tail = f". Entirely unclaimable: {named}{more}"
    return f"of {n} served point(s): {', '.join(parts)}{tail}"
