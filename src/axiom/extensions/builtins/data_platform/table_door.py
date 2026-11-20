# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.table`` and ``data.profile``: a TableSpec answered over gold (ADR-217).

Contract: ``docs/specs/spec-data-table.md`` (phase 2) and ADR-217.

A table that scrolls over a billion rows cannot fetch them, and cannot sort a
slice of them and call it sorted. So the work goes to the data:

* **Gold only** (ADR-128). The relation is resolved in the ``gold`` schema and
  nowhere else; a caller cannot name a tier. The caller's site scope, access
  tiers and the self-test hold-out are the same predicates every gold verb
  ANDs on (:mod:`.gold_query`), and row-level security on the serving role
  applies underneath them.
* **Pushdown.** ``where`` (a closed operator set), ``equals``, ``search`` and
  the multi-key ``sort`` compile to one parameterized statement. No caller
  string becomes SQL text: identifiers are checked against the introspected
  relation and quoted, values are bound and cast to the column's catalogue
  type.
* **Keyset, never deep OFFSET.** Every order is made total by the relation's
  unique row key. A window answers with ``next``, an opaque cursor holding the
  last row's order values; the next window seeks past it through the index.
  ``OFFSET`` is used only for a small offset; a large one (the scrollbar
  dragged to 73%) is an *estimated jump*: the planner's histogram gives the
  sort value at that fraction, the window seeks to it, and ``position`` says
  it is an estimate.
* **Nulls last, both ways.** A nullable leading sort key is read in two
  segments (its values, then its nulls), each an index scan in either
  direction, so ``desc`` does not need a second index to keep nulls last.
* **Sortable means indexed or declared cheap.** The leading sort key must be
  backed by a btree index (after any columns the query holds equal), or the
  relation must be small, or a deployment must declare the column cheap.
  Otherwise the sort is refused with the reason and the index that would
  serve it. :func:`check_declarations` is the conformance test of the same
  rule over declared table sources.
* **Honest counts.** ``exact`` (a bounded probe found the end), ``estimate``
  (planner statistics, or the time-series store's approximate count), or
  ``unknown``. ``count="exact"`` asks for an exact count as its own bounded
  call; one that does not finish in time stays an estimate and says why.
* **Bounded** (ADR-157). A row cap per call (``PAGE_SIZE_MAX`` and the
  caller's budget), a statement timeout, a planner-cost budget per caller
  class, and the byte budget. Anything bounded or refused says why in
  ``bounded`` / the error.

Read-only by construction: nothing here emits DDL or DML.
"""

from __future__ import annotations

import base64
import datetime as _dt
import decimal
import hashlib
import json
import re
import threading
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from axiom.extensions.builtins.scidisplay import table_spec as ts

from . import gold_query as gq

GOLD = gq.GOLD_SCHEMA

#: Rows per window, whatever the request: the spec's own ceiling.
ROW_CAP = ts.PAGE_SIZE_MAX

#: An offset at or below this is served with OFFSET; above it, an estimated jump.
OFFSET_WINDOW_MAX = 10_000

#: A count probe reads at most this many rows; finding the end inside it makes the count exact.
EXACT_COUNT_THRESHOLD = 100_000

#: A relation the planner believes at or under this many rows sorts cheaply on any column.
CHEAP_RELATION_ROWS = 10_000

#: A leading sort key whose ties average more rows than this needs an index that covers the next key too.
TIE_GROUP_MAX = 10_000

#: Above this many filtered rows a profile is computed on a sample, and says so.
PROFILE_SAMPLE_THRESHOLD = 1_000_000
#: The rows a profile sample aims for.
PROFILE_SAMPLE_ROWS = 50_000
#: Bins in a profile histogram; values in a top list.
PROFILE_BINS = 20
PROFILE_TOP = 10

#: Statement timeout per call, and the shorter one a count probe gets.
STATEMENT_TIMEOUT_MS = 5_000
PROBE_TIMEOUT_MS = 250

#: Planner cost a single window may cost, per caller class (ADR-157 "cost budget per principal":
#: admission meters each principal; this bounds what one admitted call may ask the database for).
COST_BUDGET = {"interactive": 2_000_000.0, "service": 20_000_000.0, "agent": 500_000.0, "anonymous": 50_000.0}

_TYPE_RE = re.compile(r"^[a-z][a-z0-9 _(),\[\]]*$")
_TIME_TYPES = ("timestamp with time zone", "timestamp without time zone", "date")
_NUMERIC_TYPES = ("double precision", "real", "numeric", "integer", "bigint", "smallint")


class TableDoorError(gq.GoldQueryError):
    """A caller-facing refusal: the message says why, and what would succeed.

    ``hints`` is the same advice as data a client can act on with one click (see
    :func:`refusal_hints`); it travels in the envelope's ``unmet`` entry, so a table can
    offer the fix without parsing the sentence.
    """

    def __init__(self, message: str, hints: Sequence[Mapping[str, Any]] = ()) -> None:
        super().__init__(message)
        self.hints: list[dict[str, Any]] = [dict(h) for h in hints]


# --------------------------------------------------------------------- refusals as data


def _leading_columns(rel: Relation) -> set[str]:
    return {ix.columns[0] for ix in rel.indexes if ix.columns}


def _predicate_text(column: str, op: str, value: Any = None) -> str:
    shown = ",".join(str(v) for v in value) if isinstance(value, (list, tuple)) else value
    return f"{column} {op}" + (f" {shown}" if shown is not None else "")


def refusal_hints(rel: Relation, spec: ts.TableSpec, *, sort: bool = True, filters: bool = True) -> list[dict[str, Any]]:
    """What would make a refused query succeed, as actions a client can offer.

    - ``remove_filter``: a filter the database cannot serve from an index (its column leads
      none), named by the predicate so the client removes exactly it. When every filter is
      indexed the slowness is theirs together, so each is offered.
    - ``sort_by``: an indexed column to sort by instead, keeping the requested direction and
      preferring the time column.

    Each carries a ``label`` worded for a button. The sentence in the refusal says the same
    thing for a reader of a terminal.
    """
    hints: list[dict[str, Any]] = []
    leading = _leading_columns(rel)
    f = spec.filter
    if filters and f:
        found: list[tuple[bool, dict[str, Any]]] = []
        for column, value in f.equals:
            found.append((column not in leading, {
                "action": "remove_filter", "filter": {"column": column, "op": "eq", "value": value},
                "label": f"Remove filter {_predicate_text(column, 'eq', value)}"}))
        for pred in f.where:
            doc = pred.to_document()
            found.append((pred.column not in leading, {
                "action": "remove_filter", "filter": doc,
                "label": f"Remove filter {_predicate_text(pred.column, pred.op, doc.get('value'))}"}))
        if f.search:
            found.append((True, {"action": "remove_filter", "filter": {"search": f.search},
                                 "label": f"Remove search \u201c{f.search}\u201d"}))
        slow = [h for unindexed, h in found if unindexed]
        hints += slow or [h for _, h in found]
    if sort and spec.sorts:
        lead = spec.sorts[0]
        if lead.column not in leading or len(spec.sorts) > 1:
            shown = {c.id: c for c in spec.columns}
            time_col = _time_column(rel)
            options = [c for c in leading if c in shown and c != lead.column]
            options.sort(key=lambda c: (c != time_col, c))
            if options:
                col = options[0]
                label = shown[col].label if shown[col].label else col
                hints.append({"action": "sort_by", "column": col, "direction": lead.direction,
                              "label": f"Sort by {label}"})
    return hints


# --------------------------------------------------------------------- the relation


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    not_null: bool

    @property
    def numeric(self) -> bool:
        return self.type.startswith(_NUMERIC_TYPES)

    @property
    def temporal(self) -> bool:
        return self.type.startswith(_TIME_TYPES)


@dataclass(frozen=True)
class IndexInfo:
    name: str
    unique: bool
    columns: tuple[str, ...]
    #: per column: (descending, nulls_first) as the index stores it
    options: tuple[tuple[bool, bool], ...]


@dataclass(frozen=True)
class Relation:
    table: str
    oid: int
    columns: dict[str, ColumnInfo]
    indexes: tuple[IndexInfo, ...]
    key: tuple[str, ...]
    estimated_rows: float | None
    hypertable: bool

    @property
    def qualified(self) -> str:
        return f"{GOLD}.{self.table}"

    def info(self, name: str) -> ColumnInfo:
        try:
            return self.columns[name]
        except KeyError:
            raise gq.UnknownObject(
                f"no column {name!r} in {self.qualified} (have: {', '.join(sorted(self.columns))})"
            ) from None


@dataclass(frozen=True)
class TableSourceDeclaration:
    """What a deployment or extension declares about a gold relation it serves as a table.

    ``sortable`` columns must each be indexed or listed in ``cheap``; the
    conformance check (:func:`check_declarations`) fails a declaration that
    breaks this, so a missing index fails a test, not a user. ``key`` is the
    row key for a relation with no unique index (a view).
    """

    table: str
    sortable: tuple[str, ...] = ()
    cheap: tuple[str, ...] = ()
    key: tuple[str, ...] = ()


_DECLARATIONS: dict[str, TableSourceDeclaration] = {}


def declare_table_source(decl: TableSourceDeclaration) -> None:
    if not gq._IDENT_RE.match(decl.table):
        raise TableDoorError(f"{decl.table!r} is not a table name")
    _DECLARATIONS[decl.table] = decl


def declared_table_sources() -> tuple[TableSourceDeclaration, ...]:
    return tuple(_DECLARATIONS[k] for k in sorted(_DECLARATIONS))


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def load_relation(cur, table: str) -> Relation:
    """The gold relation, its columns, btree indexes and row key, from the catalogue.

    Starts from the module's own statement budget: ``_set_timeout`` sets it for the
    session, so a caller's deliberately short budget (a bounded probe, a count asked
    to give up quickly) would otherwise leak into these catalogue reads on a shared
    or pooled connection and cancel them.
    """
    _set_timeout(cur, STATEMENT_TIMEOUT_MS)
    if not gq._IDENT_RE.match(table or ""):
        raise gq.UnknownObject(f"{table!r} is not a table name")
    cur.execute("SELECT to_regclass(%s)::oid", [f"{_q(GOLD)}.{_q(table)}"])
    row = cur.fetchone()
    oid = row[0] if row else None
    if not oid:
        raise gq.UnknownObject(f"no table {GOLD}.{table} (data.table reads gold only, ADR-128)")
    cur.execute(
        "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull "
        "FROM pg_attribute a WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attnum",
        [oid],
    )
    columns = {r[0]: ColumnInfo(r[0], r[1], bool(r[2])) for r in cur.fetchall()}
    cur.execute(
        "SELECT attnum, attname FROM pg_attribute WHERE attrelid = %s AND attnum > 0", [oid]
    )
    by_num = {int(r[0]): r[1] for r in cur.fetchall()}
    indexes = []
    cur.execute(
        "SELECT ci.relname, i.indisunique, i.indkey::int2[], i.indoption::int2[], i.indnkeyatts "
        "FROM pg_index i JOIN pg_class ci ON ci.oid = i.indexrelid JOIN pg_am am ON am.oid = ci.relam "
        "WHERE i.indrelid = %s AND am.amname = 'btree' AND i.indpred IS NULL AND i.indisvalid "
        "ORDER BY ci.relname",
        [oid],
    )
    for name, unique, keys, opts, nkey in cur.fetchall():
        keys = list(keys)[: int(nkey)]
        if any(k == 0 for k in keys):
            continue  # an expression index orders by something that is not a column
        cols = tuple(by_num[int(k)] for k in keys)
        options = tuple((bool(int(o) & 1), bool(int(o) & 2)) for o in list(opts)[: len(cols)])
        indexes.append(IndexInfo(name, bool(unique), cols, options))
    # The row key: the narrowest unique index whose columns are all NOT NULL,
    # else what a declaration says (a view has no index of its own).
    candidates = sorted(
        (ix for ix in indexes if ix.unique and all(columns[c].not_null for c in ix.columns)),
        key=lambda ix: (len(ix.columns), ix.name),
    )
    decl = _DECLARATIONS.get(table)
    key = candidates[0].columns if candidates else (decl.key if decl else ())
    cur.execute("SELECT reltuples FROM pg_class WHERE oid = %s", [oid])
    reltuples = (cur.fetchone() or [None])[0]
    hyper = False
    try:
        cur.execute(
            "SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"
        )
        if cur.fetchone():
            cur.execute(
                "SELECT 1 FROM timescaledb_information.hypertables "
                "WHERE hypertable_schema = %s AND hypertable_name = %s",
                [GOLD, table],
            )
            hyper = bool(cur.fetchone())
    except Exception:  # noqa: BLE001 - the extension is optional
        hyper = False
    estimated = None
    if hyper:
        cur.execute("SELECT approximate_row_count(%s::regclass)", [oid])
        estimated = float((cur.fetchone() or [0])[0] or 0)
    elif reltuples is not None and float(reltuples) >= 0:
        estimated = float(reltuples)
    return Relation(table, int(oid), columns, tuple(indexes), tuple(key), estimated, hyper)


# ------------------------------------------------------------------ the statement


def _cast(rel: Relation, column: str) -> str:
    typ = rel.info(column).type
    if not _TYPE_RE.match(typ):
        raise TableDoorError(f"column {column!r} has a type this door cannot bind ({typ})")
    return f"CAST(%s AS {typ})"


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def compile_predicate(rel: Relation, pred: ts.Predicate) -> tuple[str, list[Any]]:
    """One ``where`` predicate as parameterized SQL. The operator set is closed (``WHERE_OPS``)."""
    col = _q(rel.info(pred.column).name)
    op = pred.op
    if op == "is_null":
        return f"{col} IS NULL", []
    if op == "not_null":
        return f"{col} IS NOT NULL", []
    if op in ("contains", "starts_with"):
        pattern = _like_escape(str(pred.value))
        pattern = f"%{pattern}%" if op == "contains" else f"{pattern}%"
        return f"{col}::text ILIKE %s ESCAPE '\\'", [pattern]
    cast = _cast(rel, pred.column)
    if op == "between":
        return f"{col} BETWEEN {cast} AND {cast}", [pred.value[0], pred.value[1]]
    if op in ("in", "not_in"):
        marks = ", ".join(cast for _ in pred.value)
        return f"{col} {'IN' if op == 'in' else 'NOT IN'} ({marks})", list(pred.value)
    sql_op = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}[op]
    return f"{col} {sql_op} {cast}", [pred.value]


@dataclass
class Query:
    """The FROM and WHERE every statement of one request shares."""

    rel: Relation
    where: list[str] = field(default_factory=list)
    params: list[Any] = field(default_factory=list)
    described: list[str] = field(default_factory=list)
    #: columns the predicates hold to one value (so an index may start with them)
    held_equal: set[str] = field(default_factory=set)

    def clause(self, extra: Sequence[str] = ()) -> str:
        parts = [*self.where, *extra]
        return (" WHERE " + " AND ".join(f"({p})" for p in parts)) if parts else ""

    def fingerprint(self, *more: Any) -> str:
        blob = json.dumps([self.rel.qualified, self.where, [str(p) for p in self.params], *more],
                          default=str, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()


def build_query(
    rel: Relation,
    spec: ts.TableSpec,
    *,
    access_tiers: Sequence[str] = ("public",),
    restricted_tables: Iterable[str] = (),
    include_synthetic: bool = False,
    site_scope: str | None = None,
) -> Query:
    """Scope, tier and self-test hold-out (as every gold verb), then the spec's filter and window."""
    q = Query(rel)
    names = set(rel.columns)
    held = gq.scope_clause(names, site_scope, table=rel.table)
    tier = gq.tier_clause(names, access_tiers, table=rel.table, restricted_tables=restricted_tables)
    synthetic = gq.synthetic_clause(names, include_synthetic=include_synthetic)
    platform = [c for c in (held, tier, synthetic) if c is not None]
    if platform:
        sql, ps = gq.compile_where(platform, names)
        q.where.append(sql)
        q.params.extend(ps)
    if held is not None:
        q.held_equal.add(held.column)
        q.described.append(f"{held.column} = {held.values[0]!r} (held to the credential's site)")
    if tier is not None:
        q.described.append(f"access_tier in {list(tier.values)}")
    if synthetic is not None:
        q.described.append(f"{gq.BASIS_COLUMN} != {gq.SELFTEST_BASIS!r}")

    f = spec.filter
    if f:
        for column, value in f.equals:
            q.where.append(f"{_q(rel.info(column).name)} = {_cast(rel, column)}")
            q.params.append(value)
            q.held_equal.add(column)
        for pred in f.where:
            sql, ps = compile_predicate(rel, pred)
            q.where.append(sql)
            q.params.extend(ps)
            if pred.op == "eq" or (pred.op == "in" and len(pred.value) == 1):
                q.held_equal.add(pred.column)
        if f.search:
            texty = [c.id for c in spec.columns if c.id in rel.columns]
            if texty:
                q.where.append(" OR ".join(f"{_q(c)}::text ILIKE %s ESCAPE '\\'" for c in texty))
                q.params.extend([f"%{_like_escape(f.search)}%"] * len(texty))

    window = (spec.source.args.get("window") if spec.source else None) or None
    if isinstance(window, Mapping) and (window.get("from") or window.get("to")):
        wcol = window.get("column") or _time_column(rel)
        if not wcol:
            raise TableDoorError(f"{rel.qualified} has no time column for the source window")
        if window.get("from"):
            q.where.append(f"{_q(rel.info(wcol).name)} >= {_cast(rel, wcol)}")
            q.params.append(window["from"])
        if window.get("to"):
            q.where.append(f"{_q(wcol)} < {_cast(rel, wcol)}")
            q.params.append(window["to"])
        q.described.append(f"window {window.get('from')}..{window.get('to')} on {wcol}")
    return q


def _time_column(rel: Relation) -> str | None:
    for name, info in rel.columns.items():
        if info.temporal:
            return name
    return None


# --------------------------------------------------------------------- the order


@dataclass(frozen=True)
class OrderKey:
    column: str
    desc: bool
    nullable: bool
    tiebreak: bool = False


def order_keys(rel: Relation, sorts: Sequence[ts.Sort]) -> tuple[OrderKey, ...]:
    """The spec's sort, made total by the row key (ties follow the last key's direction)."""
    if not rel.key:
        raise TableDoorError(
            f"{rel.qualified} has no unique row key (no unique index over NOT NULL columns, and no "
            f"declaration), so its rows cannot be paged stably; declare one with "
            f"declare_table_source(TableSourceDeclaration(table={rel.table!r}, key=(...)))"
        )
    keys = [OrderKey(s.column, s.direction == "desc", not rel.info(s.column).not_null) for s in sorts]
    last_desc = keys[-1].desc if keys else False
    named = {k.column for k in keys}
    keys += [OrderKey(c, last_desc, False, tiebreak=True) for c in rel.key if c not in named]
    return tuple(keys)


@dataclass(frozen=True)
class Sortability:
    ok: bool
    how: str
    reason: str = ""


def _n_distinct(cur, rel: Relation, column: str) -> float | None:
    cur.execute(
        "SELECT n_distinct FROM pg_stats WHERE schemaname = %s AND tablename = %s AND attname = %s "
        "ORDER BY inherited DESC LIMIT 1",
        [GOLD, rel.table, column],
    )
    row = cur.fetchone()
    if not row or row[0] is None:
        return None
    nd = float(row[0])
    if nd < 0:
        return -nd * (rel.estimated_rows or 0)
    return nd


def sortability(cur, rel: Relation, keys: Sequence[OrderKey], held_equal: set[str]) -> Sortability:
    """Can the database produce this order without sorting the result? Indexed, cheap, or not."""
    if not keys:
        return Sortability(True, "unordered")
    lead = keys[0]
    decl = _DECLARATIONS.get(rel.table)
    if decl and lead.column in decl.cheap:
        return Sortability(True, f"{lead.column} declared cheap")
    if rel.estimated_rows is not None and rel.estimated_rows <= CHEAP_RELATION_ROWS:
        return Sortability(True, f"{rel.qualified} is small (~{int(rel.estimated_rows)} rows)")
    user_keys = [k for k in keys if not k.tiebreak] or [keys[0]]
    best = 0
    best_ix = None
    for ix in rel.indexes:
        cols = list(ix.columns)
        # Columns the query holds to one value may come first in the index.
        start = 0
        while start < len(cols) and cols[start] in held_equal and cols[start] != lead.column:
            start += 1
        matched = 0
        forward = None
        for k, col in zip(keys, cols[start:], strict=False):
            if col != k.column:
                break
            desc, _nulls_first = ix.options[start + matched]
            direction = (k.desc == desc)
            if forward is None:
                forward = direction
            elif forward != direction:
                break
            matched += 1
        if matched > best:
            best, best_ix = matched, ix
    if best == 0:
        candidates = [ix.name for ix in rel.indexes if lead.column in ix.columns]
        return Sortability(
            False, "unsupported",
            f"sorting {rel.qualified} by {lead.column} is not supported in the database: no index "
            f"starts with {lead.column}"
            + (f" (it appears later in {', '.join(candidates)})" if candidates else "")
            + f". An index on ({lead.column}) would serve it; sort by an indexed column instead",
        )
    if best >= len(user_keys):
        return Sortability(True, f"index {best_ix.name}")
    # The index orders the leading key(s); the rest are sorted within its ties.
    nd = _n_distinct(cur, rel, lead.column)
    tie = (rel.estimated_rows or 0) / nd if nd else None
    if tie is not None and tie <= TIE_GROUP_MAX:
        return Sortability(True, f"index {best_ix.name}, then ties of ~{int(tie)} rows sorted")
    return Sortability(
        False, "unsupported",
        f"sorting by {', '.join(k.column for k in user_keys)}: index {best_ix.name} orders "
        f"{lead.column}, but each of its values has ~{int(tie or 0):,} rows to sort by the next key. "
        f"An index on ({', '.join(k.column for k in user_keys)}) would serve it",
    )


def check_declarations(cur, declarations: Iterable[TableSourceDeclaration] | None = None) -> list[str]:
    """Conformance: every declared-sortable column is indexed or declared cheap. Returns the failures."""
    failures: list[str] = []
    for decl in declarations if declarations is not None else declared_table_sources():
        try:
            rel = load_relation(cur, decl.table)
        except gq.GoldQueryError as exc:
            failures.append(f"{decl.table}: {exc}")
            continue
        for column in decl.sortable:
            if column in decl.cheap:
                continue
            if column not in rel.columns:
                failures.append(f"{decl.table}.{column}: declared sortable, and the relation has no such column")
                continue
            leads = [ix.name for ix in rel.indexes if ix.columns and ix.columns[0] == column]
            if not leads:
                failures.append(
                    f"{decl.table}.{column}: declared sortable with no index starting with it and not "
                    f"declared cheap"
                )
    return failures


# ------------------------------------------------------------------ the cursor


def _json_ready(value: Any) -> Any:
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, memoryview)):
        return base64.b64encode(bytes(value)).decode()
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_ready(v) for k, v in value.items()}
    return value


def encode_cursor(query_hash: str, segment: str, values: Sequence[Any]) -> str:
    raw = json.dumps({"q": query_hash[:16], "s": segment, "v": _json_ready(list(values))},
                     separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, query_hash: str) -> tuple[str, list[Any]]:
    try:
        pad = "=" * (-len(cursor) % 4)
        doc = json.loads(base64.urlsafe_b64decode(cursor + pad))
        segment, values, qh = doc["s"], doc["v"], doc["q"]
    except Exception:  # noqa: BLE001
        raise TableDoorError("window cursor is not one this door issued") from None
    if qh != query_hash[:16]:
        raise TableDoorError(
            "window cursor belongs to a different sort or filter; start again from offset 0"
        )
    if segment not in ("all", "values", "nulls") or not isinstance(values, list):
        raise TableDoorError("window cursor is not one this door issued")
    return segment, values


# ----------------------------------------------------------- segments and keyset


def _segments(keys: Sequence[OrderKey]) -> list[str]:
    return ["values", "nulls"] if keys and keys[0].nullable else ["all"]


def _segment_keys(keys: Sequence[OrderKey], segment: str) -> tuple[list[OrderKey], list[str]]:
    """The order inside a segment, and the predicate that selects it."""
    if segment == "all":
        return list(keys), []
    lead = keys[0]
    if segment == "values":
        return [OrderKey(lead.column, lead.desc, False), *keys[1:]], [f"{_q(lead.column)} IS NOT NULL"]
    return list(keys[1:]), [f"{_q(lead.column)} IS NULL"]


def _order_sql(keys: Sequence[OrderKey]) -> str:
    # NULLS LAST only where a column can hold a null: on a NOT NULL column it
    # is a no-op the planner would still have to match against the index.
    return ", ".join(
        f"{_q(k.column)} {'DESC' if k.desc else 'ASC'}" + (" NULLS LAST" if k.nullable else "")
        for k in keys
    )


def keyset_predicate(rel: Relation, keys: Sequence[OrderKey], values: Sequence[Any]) -> tuple[str, list[Any]]:
    """Rows strictly after ``values`` in this order. Nulls sort last whichever way a key runs."""
    if len(values) != len(keys):
        raise TableDoorError("window cursor does not match this order")
    simple = all(not k.nullable for k in keys) and len({k.desc for k in keys}) == 1 and all(
        v is not None for v in values
    )
    if simple:
        # One row comparison plus the leading bound: both are index conditions.
        op = "<" if keys[0].desc else ">"
        lead_op = "<=" if keys[0].desc else ">="
        cols = ", ".join(_q(k.column) for k in keys)
        marks = ", ".join(_cast(rel, k.column) for k in keys)
        sql = f"{_q(keys[0].column)} {lead_op} {_cast(rel, keys[0].column)} AND ({cols}) {op} ({marks})"
        return sql, [values[0], *values]
    ors: list[str] = []
    params: list[Any] = []
    for i, k in enumerate(keys):
        terms: list[str] = []
        ps: list[Any] = []
        for j in range(i):
            if values[j] is None:
                terms.append(f"{_q(keys[j].column)} IS NULL")
            else:
                terms.append(f"{_q(keys[j].column)} = {_cast(rel, keys[j].column)}")
                ps.append(values[j])
        if values[i] is None:
            continue  # nothing sorts after a null: nulls are last
        after = f"{_q(k.column)} {'<' if k.desc else '>'} {_cast(rel, k.column)}"
        if k.nullable:
            after = f"({after} OR {_q(k.column)} IS NULL)"
        terms.append(after)
        ps.append(values[i])
        ors.append(" AND ".join(terms))
        params.extend(ps)
    return ("(" + " OR ".join(f"({o})" for o in ors) + ")") if ors else "FALSE", params


# ------------------------------------------------------------------- execution


def _explain(cur, sql: str, params: Sequence[Any]) -> dict[str, Any]:
    cur.execute("EXPLAIN (FORMAT JSON) " + sql, list(params))
    plan = cur.fetchone()[0]
    if isinstance(plan, str):
        plan = json.loads(plan)
    return plan[0]["Plan"]


def _set_timeout(cur, ms: int) -> None:
    cur.execute("SELECT set_config('statement_timeout', %s, false)", [f"{int(ms)}ms"])


def _is_timeout(exc: Exception) -> bool:
    code = getattr(exc, "pgcode", None)
    return code == "57014" or "statement timeout" in str(exc)


def _rollback_if_needed(cur) -> None:
    conn = getattr(cur, "connection", None)
    if conn is not None and not getattr(conn, "autocommit", True):
        conn.rollback()


def estimate_count(cur, q: Query) -> tuple[float | None, str]:
    """Planner statistics for a filtered query; the store's approximate count for an unfiltered one."""
    if not q.where and q.rel.estimated_rows is not None:
        method = "timescale approximate_row_count" if q.rel.hypertable else "planner statistics (reltuples)"
        return q.rel.estimated_rows, method
    try:
        plan = _explain(cur, f"SELECT 1 FROM {_q(GOLD)}.{_q(q.rel.table)}{q.clause()}", q.params)
        return float(plan.get("Plan Rows") or 0), "planner statistics (EXPLAIN)"
    except Exception:  # noqa: BLE001
        _rollback_if_needed(cur)
        return None, "no statistics"


def count_rows(cur, q: Query, *, exact: bool = False, timeout_ms: int = STATEMENT_TIMEOUT_MS) -> dict[str, Any]:
    """``{kind: exact|estimate|unknown, n, method, reason?}``. Never a guess shown as fact."""
    est, method = estimate_count(cur, q)
    base = f"SELECT 1 FROM {_q(GOLD)}.{_q(q.rel.table)}{q.clause()}"
    if exact:
        _set_timeout(cur, timeout_ms)
        try:
            cur.execute(f"SELECT count(*) FROM ({base}) s", q.params)
            return {"kind": "exact", "n": int(cur.fetchone()[0]), "method": "count(*)"}
        except Exception as exc:  # noqa: BLE001
            _rollback_if_needed(cur)
            if not _is_timeout(exc):
                raise
            out = _estimate_doc(est, method)
            out["reason"] = f"an exact count did not finish within {timeout_ms / 1000:g} s"
            return out
    # Probe only where the planner thinks the end is near: a probe that runs out
    # of time is latency the first window paid for nothing.
    if est is not None and est <= EXACT_COUNT_THRESHOLD * 2:
        # A probe that stops at the threshold: finding the end inside it makes the count exact.
        _set_timeout(cur, PROBE_TIMEOUT_MS)
        try:
            cur.execute(f"SELECT count(*) FROM ({base} LIMIT {EXACT_COUNT_THRESHOLD + 1}) s", q.params)
            n = int(cur.fetchone()[0])
            if n <= EXACT_COUNT_THRESHOLD:
                return {"kind": "exact", "n": n, "method": f"bounded probe (<= {EXACT_COUNT_THRESHOLD:,} rows)"}
        except Exception as exc:  # noqa: BLE001
            _rollback_if_needed(cur)
            if not _is_timeout(exc):
                raise
        finally:
            _set_timeout(cur, timeout_ms)
    return _estimate_doc(est, method)


def _estimate_doc(est: float | None, method: str) -> dict[str, Any]:
    if est is None:
        return {"kind": "unknown", "n": None, "method": method}
    return {"kind": "estimate", "n": int(round(est)), "method": method}


def _histogram_value(cur, rel: Relation, column: str, fraction: float) -> tuple[Any, str] | None:
    """The value at ``fraction`` of a column's distribution, from the planner's histogram."""
    cur.execute(
        "SELECT histogram_bounds::text, null_frac FROM pg_stats WHERE schemaname = %s AND tablename = %s "
        "AND attname = %s ORDER BY inherited DESC LIMIT 1",
        [GOLD, rel.table, column],
    )
    row = cur.fetchone()
    if row and row[0]:
        cur.execute(f"SELECT CAST(%s AS {rel.info(column).type}[])", [row[0]])
        bounds = list(cur.fetchone()[0] or [])
        if bounds:
            i = min(len(bounds) - 1, max(0, int(round(fraction * (len(bounds) - 1)))))
            return bounds[i], "planner histogram"
    return None


@dataclass
class WindowResult:
    rows: list[dict[str, Any]]
    keys: list[str]
    next: str | None
    position: dict[str, Any]
    statements: list[str]


def _select_list(rel: Relation, spec: ts.TableSpec, keys: Sequence[OrderKey]) -> list[str]:
    wanted: list[str] = []
    for c in spec.columns:
        rel.info(c.id)
        wanted.append(c.id)
        for ref in (c.uncertainty_from, c.option("unit_from"), c.option("reason_from"), c.option("quality_from")):
            if ref and ref in rel.columns:
                wanted.append(ref)
        if c.type == "quantity":
            for default in (ts.QUALITY_FROM_DEFAULT, ts.REASON_FROM_DEFAULT):
                if default in rel.columns:
                    wanted.append(default)
        if c.type == "quality" and ts.REASON_FROM_DEFAULT in rel.columns:
            wanted.append(ts.REASON_FROM_DEFAULT)
    wanted += [k.column for k in keys]
    return list(dict.fromkeys(wanted))


def fetch_window(
    cur,
    q: Query,
    spec: ts.TableSpec,
    keys: Sequence[OrderKey],
    *,
    limit: int,
    offset: int = 0,
    cursor: str | None = None,
    count_hint: float | None = None,
    cost_budget: float | None = None,
) -> WindowResult:
    """One window: keyset after ``cursor``, a small OFFSET, or an estimated jump."""
    cols = _select_list(q.rel, spec, keys)
    select = ", ".join(_q(c) for c in cols)
    frm = f"{_q(GOLD)}.{_q(q.rel.table)}"
    qhash = q.fingerprint([(k.column, k.desc) for k in keys])
    segments = _segments(keys)
    position: dict[str, Any] = {"kind": "exact", "offset": offset}
    statements: list[str] = []

    start_segment = segments[0]
    after: list[Any] | None = None
    seek: tuple[str, list[Any]] | None = None
    skip = 0
    if cursor:
        start_segment, after = decode_cursor(cursor, qhash)
        position = {"kind": "keyset", "cursor": True}
    elif offset and offset <= OFFSET_WINDOW_MAX:
        skip = offset
    elif offset:
        # An estimated jump: the sort value at this fraction, then a keyset seek to it.
        lead = keys[0]
        total = count_hint or q.rel.estimated_rows or 0
        fraction = min(1.0, offset / total) if total else 0.0
        found = _histogram_value(cur, q.rel, lead.column, 1 - fraction if lead.desc else fraction)
        if found is None:
            raise TableDoorError(
                f"no statistics for {lead.column} to estimate a jump to row {offset:,}; "
                f"page forward with the cursor instead"
            )
        value, method = found
        op = "<=" if lead.desc else ">="
        seek = (f"{_q(lead.column)} {op} {_cast(q.rel, lead.column)}", [value])
        position = {
            "kind": "estimate", "offset": offset, "method": method,
            "seek": {"column": lead.column, "value": _json_ready(value)},
            "note": f"jumped to about row {offset:,} by the {method} of {lead.column}; "
                    f"the rows from here on are exact, the row number is not",
        }
        if lead.nullable:
            start_segment = "values"

    rows: list[dict[str, Any]] = []
    last_segment = start_segment
    began = segments.index(start_segment)
    for seg in segments[began:]:
        need = limit + 1 - len(rows)
        if need <= 0:
            break
        seg_keys, seg_where = _segment_keys(keys, seg)
        extra = list(seg_where)
        params = list(q.params)
        if seg == start_segment and after is not None:
            vals = after if seg != "nulls" else after[1:]
            pred, ps = keyset_predicate(q.rel, seg_keys, vals)
            extra.append(pred)
            params += ps
        if seg == start_segment and seek is not None:
            extra.append(seek[0])
            params += seek[1]
        order = _order_sql(seg_keys)
        sql = f"SELECT {select} FROM {frm}{q.clause(extra)}" + (f" ORDER BY {order}" if order else "")
        sql += f" LIMIT {int(need)}"
        if skip:
            sql += f" OFFSET {int(skip)}"
        if cost_budget is not None:
            plan = _explain(cur, sql, params)
            cost = float(plan.get("Total Cost") or 0)
            if cost > cost_budget:
                raise TableDoorError(
                    f"refused: this window would cost the database {cost:,.0f} (planner units), over the "
                    f"budget of {cost_budget:,.0f} for this caller (ADR-157); narrow the filter, or sort "
                    f"by an indexed column",
                    refusal_hints(q.rel, spec),
                )
        statements.append(sql)
        cur.execute(sql, params)
        fetched = cur.fetchall()
        names = [d[0] for d in cur.description]
        if skip:
            if not fetched:
                # The offset ran past this segment; how far, so the next segment skips the rest.
                cur.execute(
                    f"SELECT count(*) FROM (SELECT 1 FROM {frm}{q.clause(seg_where)} LIMIT {int(skip)}) s",
                    list(q.params),
                )
                skip -= int(cur.fetchone()[0])
            else:
                skip = 0
        for r in fetched:
            rows.append(dict(zip(names, r, strict=False)))
            last_segment = seg
        if len(rows) > limit:
            break

    more = len(rows) > limit
    rows = rows[:limit]
    nxt = None
    if more and rows:
        last = rows[-1]
        nxt = encode_cursor(qhash, last_segment if len(segments) > 1 else "all",
                            [last[k.column] for k in keys])
    out_rows = [{k: _json_ready(v) for k, v in r.items()} for r in rows]
    out_keys = [json.dumps([_json_ready(r.get(c)) for c in q.rel.key], separators=(",", ":"), default=str)
                for r in rows]
    return WindowResult(out_rows, out_keys, nxt, position, statements)


# --------------------------------------------------------------- the answer


def _uncertainty(spec: ts.TableSpec, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Per-row uncertainty coverage of the window, every field present (the serving envelope's rule)."""
    columns: dict[str, Any] = {}
    for c in spec.columns:
        if not c.uncertainty_from:
            continue
        values = [r.get(c.uncertainty_from) for r in rows if r.get(c.id) is not None]
        quantified = sum(1 for v in values if isinstance(v, (int, float)) and v > 0)
        columns[c.id] = {
            "from": c.uncertainty_from,
            "quantified": quantified,
            "unquantified": len(values) - quantified,
        }
    claimable = any(v["quantified"] for v in columns.values())
    if not columns:
        note = "no column of this table declares where its uncertainty comes from, so none is claimed"
    elif claimable:
        note = ("each value carries its own uncertainty in the row; a zero or missing one is unstated, "
                "never exact")
    else:
        note = "the declared uncertainty columns are empty in this window, so none is claimed"
    return {"kind": "per_row", "claimable": claimable, "columns": columns, "note": note}


def answer(
    cur,
    spec: ts.TableSpec,
    *,
    caller_class: str = "interactive",
    max_points: int = ROW_CAP,
    want_count: bool | None = None,
    exact_count: bool = False,
    access_tiers: Sequence[str] = ("public",),
    restricted_tables: Iterable[str] = (),
    include_synthetic: bool = False,
    site_scope: str | None = None,
    timeout_ms: int = STATEMENT_TIMEOUT_MS,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Answer a door TableSpec: one window, with what was applied, counted and bounded."""
    if not spec.is_door:
        raise TableDoorError("data.table answers a spec whose source is {type: verb, verb: data.table}")
    if spec.source.verb != "data.table":
        raise TableDoorError(f"this door answers data.table, not {spec.source.verb}")
    if spec.group is not None:
        raise TableDoorError("refused: grouping is not served yet (data table phase 4); remove 'group'")
    dataset = spec.source.args.get("dataset")
    if not isinstance(dataset, str) or not dataset:
        raise TableDoorError("source.args.dataset must name a gold table")

    _set_timeout(cur, timeout_ms)
    rel = load_relation(cur, dataset)
    q = build_query(rel, spec, access_tiers=access_tiers, restricted_tables=restricted_tables,
                    include_synthetic=include_synthetic, site_scope=site_scope)
    keys = order_keys(rel, spec.sorts)
    if spec.sorts:
        sorted_ok = sortability(cur, rel, [k for k in keys if not k.tiebreak], q.held_equal)
    else:
        sorted_ok = Sortability(True, f"row key {', '.join(rel.key)}")
    if not sorted_ok.ok:
        raise TableDoorError(f"refused: {sorted_ok.reason}", refusal_hints(rel, spec, filters=False))

    win = spec.window or ts.Window()
    bounded: list[str] = []
    limit = min(win.limit, ROW_CAP, max(1, int(max_points)))
    if limit < win.limit:
        bounded.append(
            f"window limited to {limit} rows (ADR-157 budget for {caller_class} callers; asked {win.limit})"
        )

    count = None
    first = win.cursor is None and win.offset == 0
    if want_count if want_count is not None else (first or exact_count):
        count = count_rows(cur, q, exact=exact_count, timeout_ms=timeout_ms)
        if count.get("reason"):
            bounded.append(count["reason"])

    try:
        window = fetch_window(
            cur, q, spec, keys, limit=limit, offset=win.offset, cursor=win.cursor,
            count_hint=(count or {}).get("n"), cost_budget=COST_BUDGET.get(caller_class),
        )
    except Exception as exc:  # noqa: BLE001
        if _is_timeout(exc):
            _rollback_if_needed(cur)
            raise TableDoorError(
                f"refused: the window did not finish within {timeout_ms / 1000:g} s (statement timeout, "
                f"ADR-157); narrow the filter or sort by an indexed column",
                refusal_hints(rel, spec),
            ) from None
        raise
    if window.position.get("kind") == "estimate":
        bounded.append(window.position["note"])

    if count is None and not window.next:
        # The last window: its end IS the count, whatever was estimated before.
        if win.cursor is None and window.position.get("kind") == "exact":
            count = {"kind": "exact", "n": win.offset + len(window.rows), "method": "end of result reached"}

    applied = {
        "sort": [{"column": k.column, "direction": "desc" if k.desc else "asc", "nulls": "last"}
                 for k in keys if not k.tiebreak],
        "tiebreak": [k.column for k in keys if k.tiebreak],
        "order": sorted_ok.how,
        "filter": spec.filter.to_document() if spec.filter else {},
        "scope": list(q.described),
        "window": {"offset": win.offset, "limit": limit, "cursor": bool(win.cursor)},
        "pushdown": "sort, filter and window run in the database; no rows are sorted or filtered here",
    }
    reads = [{
        "tool": "data.table",
        "args": {
            "dataset": dataset,
            "sort": applied["sort"],
            "filter": applied["filter"],
            "window": applied["window"],
        },
        "relation": rel.qualified,
        "rows": len(window.rows),
    }]
    out: dict[str, Any] = {
        "rows": window.rows,
        "keys": window.keys,
        "next": window.next,
        "count": count,
        "position": window.position,
        "applied": applied,
        "bounded": bounded,
        "provenance": {
            "source": rel.qualified,
            "method": "keyset window over gold (ADR-217)",
            "rows": len(window.rows),
            "reads": reads,
            "as_of": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
            "watermark": watermark(cur, rel),
        },
        "uncertainty": _uncertainty(spec, window.rows),
    }
    if max_bytes:
        from axiom.infra import serving_guard as sgm

        out, cut = sgm.bound_bytes(out, max_bytes=max_bytes)
        if cut:
            out.setdefault("bounded", []).append(cut)
    return out


# --------------------------------------------------------------- watermark + profile


def watermark(cur, rel: Relation) -> str | None:
    """A token that changes when the relation's rows change: its write counters, chunks included.

    ``None`` for a relation the statistics collector does not track (a view);
    a profile of one is then not cached, and says so.
    """
    cur.execute(
        "SELECT count(*), coalesce(sum(n_tup_ins + n_tup_upd + n_tup_del), 0), "
        "coalesce(sum(n_live_tup), 0) FROM pg_stat_all_tables "
        "WHERE relid = %s OR relid IN (SELECT inhrelid FROM pg_inherits WHERE inhparent = %s)",
        [rel.oid, rel.oid],
    )
    n, writes, live = cur.fetchone()
    if not n:
        return None
    return f"w{int(writes)}.{int(live)}"


_PROFILE_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_PROFILE_CACHE_MAX = 512
_PROFILE_LOCK = threading.Lock()


def _profile_cached(key: str) -> dict[str, Any] | None:
    with _PROFILE_LOCK:
        hit = _PROFILE_CACHE.get(key)
        if hit is not None:
            _PROFILE_CACHE.move_to_end(key)
        return hit


def _profile_store(key: str, value: dict[str, Any]) -> None:
    with _PROFILE_LOCK:
        _PROFILE_CACHE[key] = value
        _PROFILE_CACHE.move_to_end(key)
        while len(_PROFILE_CACHE) > _PROFILE_CACHE_MAX:
            _PROFILE_CACHE.popitem(last=False)


def profile(
    cur,
    spec: ts.TableSpec,
    column: str,
    *,
    access_tiers: Sequence[str] = ("public",),
    restricted_tables: Iterable[str] = (),
    include_synthetic: bool = False,
    site_scope: str | None = None,
    timeout_ms: int = STATEMENT_TIMEOUT_MS,
    sample_threshold: int = PROFILE_SAMPLE_THRESHOLD,
) -> dict[str, Any]:
    """One column's shape over the FILTERED query; sampled above a threshold and labelled."""
    if not spec.is_door:
        raise TableDoorError("data.profile answers a door spec (source.type = verb)")
    dataset = spec.source.args.get("dataset")
    _set_timeout(cur, STATEMENT_TIMEOUT_MS)
    rel = load_relation(cur, str(dataset or ""))
    info = rel.info(column)
    declared = next((c for c in spec.columns if c.id == column), None)
    q = build_query(rel, spec, access_tiers=access_tiers, restricted_tables=restricted_tables,
                    include_synthetic=include_synthetic, site_scope=site_scope)
    mark = watermark(cur, rel)
    qhash = q.fingerprint(column, sample_threshold)
    cache_key = f"{qhash}:{mark}"
    if mark is not None and (hit := _profile_cached(cache_key)) is not None:
        return {**hit, "cache": {"hit": True, "key": qhash[:12], "watermark": mark}}

    try:
        # The bound applies to the profile's own statements, not the catalogue lookups.
        _set_timeout(cur, timeout_ms)
        out = _profile(cur, rel, info, declared, q, column, sample_threshold, mark, qhash, spec)
    except Exception as exc:  # noqa: BLE001
        if not _is_timeout(exc):
            raise
        _rollback_if_needed(cur)
        raise TableDoorError(
            f"refused: profiling {column} over this filter did not finish within {timeout_ms / 1000:g} s "
            f"(statement timeout, ADR-157). The filter is not served by an index, so every row would be "
            f"read; narrow it by an indexed column",
            refusal_hints(rel, spec, sort=False),
        ) from None
    if mark is not None:
        _profile_store(cache_key, out)
        cache = {"hit": False, "key": qhash[:12], "watermark": mark}
    else:
        cache = {"hit": False, "key": qhash[:12], "watermark": None,
                 "note": "not cached: this relation has no gold watermark (a view is not tracked)"}
    return {**out, "cache": cache}


def _profile(cur, rel: Relation, info: ColumnInfo, declared, q: Query, column: str,
             sample_threshold: int, mark: str | None, qhash: str, spec: ts.TableSpec) -> dict[str, Any]:
    est, method = estimate_count(cur, q)
    frm = f"{_q(GOLD)}.{_q(rel.table)}"
    sampled = est is not None and est > sample_threshold
    sample_sql = ""
    if sampled:
        pct = min(100.0, max(0.0001, PROFILE_SAMPLE_ROWS / est * 100.0))
        # Block sampling, seeded: the same query and watermark profile the same sample.
        sample_sql = f" TABLESAMPLE SYSTEM ({pct:.6f}) REPEATABLE (217)"
    base = f"SELECT {_q(column)} AS v FROM {frm}{sample_sql}{q.clause()}"
    col_type = declared.type if declared and declared.type else ("number" if info.numeric else
                                                                  "time" if info.temporal else "text")
    out: dict[str, Any] = {"column": column, "type": col_type, "unit": (declared.unit if declared else "") or None}

    cur.execute(f"SELECT count(*), count(v) FROM ({base}) s", q.params)
    rows, present = (int(x) for x in cur.fetchone())
    out["nulls"] = (rows - present) / rows if rows else 0.0
    if sampled:
        out["n"] = int(round(est))
        out["n_kind"] = "estimate"
    else:
        out["n"] = rows
        out["n_kind"] = "exact"

    if info.numeric or info.temporal:
        expr = "extract(epoch from v)::float8" if info.temporal else "v::float8"
        cur.execute(
            f"SELECT min({expr}), max({expr}), avg({expr}), "
            f"percentile_cont(ARRAY[0.05, 0.25, 0.5, 0.75, 0.95]) WITHIN GROUP (ORDER BY {expr}), "
            f"count(DISTINCT v) FROM ({base}) s",
            q.params,
        )
        lo, hi, mean, quantiles, distinct = cur.fetchone()
        to_out = (lambda x: _dt.datetime.fromtimestamp(x, _dt.UTC).isoformat()) if info.temporal \
            else (lambda x: x)
        out.update({
            "min": to_out(lo) if lo is not None else None,
            "max": to_out(hi) if hi is not None else None,
            "mean": to_out(mean) if mean is not None else None,
            "quantiles": [to_out(v) for v in (quantiles or [])],
        })
        if lo is not None and hi is not None and hi > lo:
            cur.execute(
                f"SELECT width_bucket({expr}, %s, %s, {PROFILE_BINS}) b, count(*) FROM ({base}) s "
                f"WHERE v IS NOT NULL GROUP BY b ORDER BY b",
                [lo, hi, *q.params],
            )
            counts = [0] * PROFILE_BINS
            for b, n in cur.fetchall():
                counts[min(PROFILE_BINS, max(1, int(b))) - 1] += int(n)
            step = (hi - lo) / PROFILE_BINS
            out["histogram"] = {"edges": [to_out(lo + i * step) for i in range(PROFILE_BINS + 1)],
                                "counts": counts}
        else:
            out["histogram"] = None
        out["top"] = None
    else:
        cur.execute(
            f"SELECT v::text, count(*) n FROM ({base}) s WHERE v IS NOT NULL GROUP BY v "
            f"ORDER BY n DESC, v LIMIT {PROFILE_TOP}",
            q.params,
        )
        out["top"] = [{"value": v, "n": int(n)} for v, n in cur.fetchall()]
        cur.execute(f"SELECT count(DISTINCT v) FROM ({base}) s", q.params)
        distinct = int(cur.fetchone()[0])
        out["histogram"] = None
    out["distinct"] = {"kind": "estimate" if sampled else "exact", "n": int(distinct or 0)}

    if "quality" in rel.columns:
        cur.execute(
            f"SELECT count(*) FILTER (WHERE quality = 'bad'), count(*) FILTER (WHERE quality = 'suspect') "
            f"FROM {frm}{sample_sql}{q.clause()}",
            q.params,
        )
        bad, suspect = cur.fetchone()
        out["flags"] = {"bad": int(bad), "suspect": int(suspect)}
    else:
        out["flags"] = None
    out["basis"] = {
        "kind": "sample" if sampled else "exact",
        "rows": rows,
        "method": (f"block sample (TABLESAMPLE SYSTEM, seeded) of ~{rows:,} rows from ~{int(est):,} "
                   f"filtered ({method})") if sampled else "every filtered row",
    }
    if sampled:
        out["basis"]["label"] = f"sample of {rows:,}"
    out["provenance"] = {
        "source": rel.qualified,
        "method": "column profile over the filtered query (ADR-217)",
        "reads": [{"tool": "data.profile", "args": {"dataset": rel.table, "column": column,
                                                    "filter": spec.filter.to_document() if spec.filter else {}},
                   "relation": rel.qualified, "rows": rows}],
        "watermark": mark,
        "scope": list(q.described),
    }
    return out


__all__ = [
    "COST_BUDGET",
    "EXACT_COUNT_THRESHOLD",
    "OFFSET_WINDOW_MAX",
    "PROFILE_SAMPLE_THRESHOLD",
    "ROW_CAP",
    "TableDoorError",
    "TableSourceDeclaration",
    "answer",
    "build_query",
    "check_declarations",
    "compile_predicate",
    "count_rows",
    "declare_table_source",
    "declared_table_sources",
    "decode_cursor",
    "encode_cursor",
    "fetch_window",
    "keyset_predicate",
    "load_relation",
    "order_keys",
    "profile",
    "sortability",
    "watermark",
]
