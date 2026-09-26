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
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

#: The medallion's served tier. Introspection is scoped to it — these verbs
#: cannot be pointed at another schema by a caller.
GOLD_SCHEMA = "gold"

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


class GoldQueryError(ValueError):
    """Base for every caller-facing error from this module."""


class UnknownObject(GoldQueryError):
    """A table, column or aggregate the introspected schema does not have."""


class FilterSyntaxError(GoldQueryError):
    """The filter (or bucket) did not parse under the restricted grammar."""


class AccessTierUnavailable(GoldQueryError):
    """A restricted table cannot be tier-filtered — refuse rather than answer."""


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
                f"cannot parse {part!r}; expected `<column> <op> <value>` "
                f"with op in {sorted(_OPS)}"
            )
    return tuple(clauses)


def compile_where(
    clauses: Sequence[Clause], allowed_columns: Iterable[str], placeholder: str = "%s"
) -> tuple[str, list[Any]]:
    """Render clauses to a parameterized WHERE fragment.

    Every identifier is checked against ``allowed_columns`` (the introspected
    set) and then double-quoted; every value becomes a bound parameter.
    """
    allowed = set(allowed_columns)
    sql_parts: list[str] = []
    params: list[Any] = []
    for c in clauses:
        if c.column not in allowed:
            raise UnknownObject(f"no column {c.column!r} (have: {', '.join(sorted(allowed))})")
        ident = '"' + c.column.replace('"', '""') + '"'
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
    tiers: Sequence[str],
    *,
    table: str,
    restricted_tables: Iterable[str] = (),
) -> Clause | None:
    """The tier predicate for ``table``, or ``None`` when tiering does not apply.

    Fail-closed: a table declared restricted that carries no ``access_tier``
    column raises rather than answering unfiltered. A generic verb must never
    widen access relative to the curated verbs it generalizes.
    """
    cols = set(columns)
    if "access_tier" in cols:
        grants = tuple(tiers)
        if not grants:
            # An empty grant set renders as ``IN ()`` — a Postgres syntax error,
            # so the deny would surface as a database error instead of a typed
            # one. "You hold no tiers" is also a different answer from "no rows
            # matched", and the caller needs to be able to tell them apart.
            raise AccessTierUnavailable(
                f"no access tiers granted for {GOLD_SCHEMA}.{table}; refusing to "
                "answer rather than answering unfiltered"
            )
        return Clause(column="access_tier", op="in", values=grants)
    if table in set(restricted_tables):
        raise AccessTierUnavailable(
            f"{GOLD_SCHEMA}.{table} is declared restricted but exposes no access_tier "
            "column; refusing to answer rather than answering unfiltered"
        )
    return None


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
) -> dict[str, Any]:
    """The standard result envelope the provenance gate + analytics consume."""
    prov: dict[str, Any] = {"source": source, "method": method}
    if rows is not None:
        prov["rows"] = rows
    if note:
        prov["note"] = note
    out: dict[str, Any] = {"data": data, "provenance": prov}
    if unit:
        out["unit"] = unit
    return out


# ---------------------------------------------------------------------------
# introspection + execution (cursor-injected, so it is testable without a DB)
# ---------------------------------------------------------------------------

_COLUMNS_SQL = (
    "SELECT column_name, data_type FROM information_schema.columns "
    "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position"
)
_TABLES_SQL = (
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema = %s ORDER BY table_name"
)


def _quote(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _columns_of(cur, table: str) -> list[tuple[str, str]]:
    if not _IDENT_RE.match(table or ""):
        raise UnknownObject(f"{table!r} is not a table name")
    cur.execute(_COLUMNS_SQL, [GOLD_SCHEMA, table])
    rows = list(cur.fetchall() or [])
    if not rows:
        raise UnknownObject(f"no table {GOLD_SCHEMA}.{table}")
    return [(r[0], r[1]) for r in rows]


def list_tables(cur) -> dict[str, Any]:
    """Every table/view in the gold tier."""
    cur.execute(_TABLES_SQL, [GOLD_SCHEMA])
    tables = [{"table": r[0]} for r in (cur.fetchall() or [])]
    return envelope(
        data={"schema": GOLD_SCHEMA, "tables": tables},
        source=GOLD_SCHEMA,
        method="information_schema.tables",
        rows=len(tables),
    )


def describe(cur, table: str) -> dict[str, Any]:
    """Columns + types for one gold table."""
    cols = _columns_of(cur, table)
    return envelope(
        data={
            "table": table,
            "columns": [{"column": name, "type": dtype} for name, dtype in cols],
        },
        source=f"{GOLD_SCHEMA}.{table}",
        method="information_schema.columns",
        rows=len(cols),
    )


def _window_and_filter(
    names: set[str],
    *,
    window: dict[str, Any] | None,
    filter: str | None,
    tiers: Sequence[str],
    table: str,
    restricted_tables: Iterable[str],
) -> tuple[list[str], list[Any], list[str]]:
    """Build the shared WHERE pieces: caller filter, time window, tier guard."""
    wheres: list[str] = []
    params: list[Any] = []
    described: list[str] = []

    clauses = list(parse_filter(filter))
    tier = tier_clause(names, tiers, table=table, restricted_tables=restricted_tables)
    if tier is not None:
        clauses.append(tier)
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


def aggregate(
    cur,
    *,
    table: str,
    column: str,
    fn: str,
    window: dict[str, Any] | None = None,
    filter: str | None = None,
    tiers: Sequence[str] = ("public",),
    restricted_tables: Iterable[str] = (),
) -> dict[str, Any]:
    """One deterministic aggregate of ``column`` over ``table``.

    Empty window → ``data: null`` with a provenance note. Never a fabricated 0
    (``count`` of nothing is still a real 0 and is reported as such).
    """
    cols = _columns_of(cur, table)
    names = {n for n, _ in cols}
    if column not in names:
        raise UnknownObject(f"no column {column!r} on {GOLD_SCHEMA}.{table}")
    agg = sql_for_fn(fn)

    wheres, params, described = _window_and_filter(
        names, window=window, filter=filter, tiers=tiers,
        table=table, restricted_tables=restricted_tables,
    )
    where_sql = f" WHERE {' AND '.join(wheres)}" if wheres else ""
    # count(col) counts VALUES; count(*) counts ROWS. Without both, "the window
    # was empty" and "every row had a null here" are indistinguishable, and they
    # call for opposite responses from the caller.
    sql = (
        f"SELECT {agg}({_quote(column)}), count({_quote(column)}), count(*) "
        f"FROM {_quote(GOLD_SCHEMA)}.{_quote(table)}{where_sql}"
    )
    cur.execute(sql, params)
    row = cur.fetchone() or (None, 0, 0)
    value, n = row[0], int(row[1] or 0)
    matched = int(row[2] or 0) if len(row) > 2 else n
    method = f"{agg}({column})" + ("; " + "; ".join(described) if described else "")
    source = f"{GOLD_SCHEMA}.{table}"

    if value is None and fn != "count":
        note = (
            "no rows matched; no value to report"
            if matched == 0
            else f"{matched} row(s) matched but {column!r} is null in all of them; "
            "no value to report"
        )
        return envelope(data=None, source=source, method=method, rows=n, note=note)
    return envelope(
        data={"table": table, "column": column, "fn": fn, "value": value},
        source=source, method=method, rows=n,
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
    tiers: Sequence[str] = ("public",),
    restricted_tables: Iterable[str] = (),
) -> dict[str, Any]:
    """A bucketed series — the input shape the analytics tool consumes."""
    if not _INTERVAL_RE.match((bucket or "").strip()):
        raise FilterSyntaxError(
            f"bucket {bucket!r} must be an interval like '1 hour' or '15 minutes'"
        )
    cols = _columns_of(cur, table)
    names = {n for n, _ in cols}
    for needed in (column, time_column):
        if needed not in names:
            raise UnknownObject(f"no column {needed!r} on {GOLD_SCHEMA}.{table}")
    agg = sql_for_fn(fn)

    wheres, params, described = _window_and_filter(
        names, window=window, filter=filter, tiers=tiers,
        table=table, restricted_tables=restricted_tables,
    )
    where_sql = f" WHERE {' AND '.join(wheres)}" if wheres else ""
    # The bucket is regex-validated against a closed interval grammar, then
    # bound as a parameter to date_bin's interval argument.
    sql = (
        f"SELECT date_bin(%s::interval, {_quote(time_column)}, TIMESTAMP 'epoch') AS bucket, "
        f"{agg}({_quote(column)}) "
        f"FROM {_quote(GOLD_SCHEMA)}.{_quote(table)}{where_sql} "
        f"GROUP BY 1 ORDER BY 1"
    )
    cur.execute(sql, [bucket, *params])
    rows = list(cur.fetchall() or [])
    points = [{"t": r[0], "value": r[1]} for r in rows]
    method = f"{agg}({column}) by {bucket}" + ("; " + "; ".join(described) if described else "")
    source = f"{GOLD_SCHEMA}.{table}"
    if not points:
        return envelope(
            data=None, source=source, method=method, rows=0,
            note="no rows matched; no series to report",
        )
    return envelope(
        data={"table": table, "column": column, "bucket": bucket, "fn": fn, "series": points},
        source=source, method=method, rows=len(points),
    )


__all__ = [
    "AGGREGATE_FNS",
    "AccessTierUnavailable",
    "Clause",
    "FilterSyntaxError",
    "GOLD_SCHEMA",
    "GoldQueryError",
    "UnknownObject",
    "aggregate",
    "compile_where",
    "describe",
    "envelope",
    "list_tables",
    "parse_filter",
    "series",
    "sql_for_fn",
    "tier_clause",
]
