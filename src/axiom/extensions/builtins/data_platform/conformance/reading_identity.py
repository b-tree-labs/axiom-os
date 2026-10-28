# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Silver's identity is the READING, not the bytes it arrived in (ADR-206).

``silver.signals`` was keyed on ``(row_hash, channel)``, and ``row_hash`` hashes
a transport artifact: the bronze row for the row path, the deposit FILE's
content hash plus ``ts`` for the deposit path. So one reading that arrived
twice — in two deposit files, or once through the row path and once as a
deposit — had two hashes and landed twice, and a re-conform after a logic fix
could only re-derive a row whose bytes it re-read under the same hash.

The identity is now ``(site, feed, channel, ts, source_class)``, enforced by a
unique index, and every conform write is an upsert on it:

* identical content           → nothing is written (``unchanged``);
* different value or metadata → the row is replaced in place (``replaced``);
* a reading not yet in silver → inserted (``inserted``).

``row_hash`` stays, as an ATTRIBUTE: it names the bronze row the reading first
arrived as, and the structured-uncertainty companion still joins on it. It is
never rewritten on a replace, so that join and the old primary key both hold.

## Expand, then contract (ADR-182)

This module is the EXPAND half. The new columns are added by the ordinary DDL
and the index is created there only when ``silver.signals`` is empty (a fresh
install). A populated node keeps writing exactly as before until an operator
has run :func:`preflight`, :func:`collapse` and :func:`build_index` — the
writer probes for the index (:func:`has_identity_key`) and uses the identity
upsert only once it is there and valid. Building a unique index on a table of
hundreds of millions of rows is never something a deploy does implicitly.

The CONTRACT half (dropping the ``(row_hash, channel[, ts])`` primary key once
nothing conflicts on it) is a later release, marked ``# contract:`` below.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

#: What makes two rows the same reading.
IDENTITY_COLUMNS = ("site", "feed", "channel", "ts", "source_class")

#: The unique index that enforces it. Includes ``ts``, which TimescaleDB
#: requires of every unique index on a hypertable.
IDENTITY_INDEX = "signals_reading_identity"

#: Where :func:`collapse` puts every row it removes. Nothing is deleted
#: without a copy: a "duplicate" that turns out to have been a real reading
#: can be put back.
AUDIT_TABLE = "silver.signals_identity_audit"

#: The reading's CONTENT: what a consumer reads off the row. A change in any of
#: these is a replace; identical values are unchanged.
#:
#: Not in it, deliberately: ``schema_ref`` and ``conform_version``. They say
#: which path and which code produced the row, not what the reading is. Two
#: paths carrying the same value are one unchanged reading, and the first
#: path keeps its provenance; counting them as a replace would rewrite a
#: tuple per reading for nothing, every time either path re-conformed.
CONTENT_COLUMNS = (
    "value",
    "unit",
    "quality",
    "quality_reason",
    "model_ref",
    "basis",
    "uncertainty",
    "role",
    "derivation",
)

#: What a replace writes: the content, plus the stamps of the conform that
#: produced it. Never an identity column and never ``row_hash``.
REPLACE_COLUMNS = (*CONTENT_COLUMNS, "schema_ref", "content_hash", "conform_version", "conformed_at")

#: Columns this release adds, nullable with no default, so adding them is a
#: catalogue edit on a table of any size rather than a rewrite. NULL means
#: "conformed before reading identity", which is a true statement about every
#: existing row.
STAMP_COLUMNS = ("content_hash", "conform_version", "conformed_at")

#: TimescaleDB versions measured against the operations here, 2026-10-27.
#: 2.17.2 refuses ANY unique index on a hypertable with compression enabled
#: ("operation not supported on hypertables that have compression enabled");
#: 2.30.2 builds it, checks uniqueness inside compressed chunks, and upserts
#: into them. Neither allows CONCURRENTLY on a hypertable, and neither allows
#: ``timescaledb.transaction_per_chunk`` with UNIQUE.
TIMESCALE_REFUSES_UNIQUE_ON_COMPRESSED = ((2, 17, 2),)
TIMESCALE_VERIFIED_UNIQUE_ON_COMPRESSED = (2, 30, 2)


# --- the content hash -------------------------------------------------------


def _canonical_number(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return value


def _terms(terms: Any) -> list[list[Any]]:
    if not terms:
        return []
    out = []
    for symbol, spec in sorted(dict(terms).items(), key=lambda kv: str(kv[0])):
        if isinstance(spec, (tuple, list)):
            coefficient, independent = spec[0], bool(spec[1]) if len(spec) > 1 else False
        elif isinstance(spec, Mapping):
            coefficient, independent = spec.get("coefficient"), bool(spec.get("independent", False))
        else:
            coefficient, independent = spec, False
        out.append([str(symbol), _canonical_number(coefficient), independent])
    return out


def content_hash(row: Mapping[str, Any]) -> str:
    """The hash of a reading's content: :data:`CONTENT_COLUMNS` plus its
    structured uncertainty terms. Identity and provenance are not in it.

    Numbers are hashed as floats, so ``1`` and ``1.0`` from two normalizers
    are the same reading, as they are in a ``double precision`` column.
    """
    body = {c: _canonical_number(row.get(c)) for c in CONTENT_COLUMNS}
    body["uncertainty_terms"] = _terms(row.get("uncertainty_terms"))
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalizer_version(fn: Any) -> str | None:
    """What version of conform logic produced a row.

    A normalizer may declare ``conform_version`` (a string it bumps when its
    logic changes). Otherwise its module's ``__version__``, otherwise just its
    qualified name. Read from attributes only: resolving a distribution through
    ``importlib.metadata`` can hang on Python 3.14, and this runs per pass.
    """
    declared = getattr(fn, "conform_version", None)
    if declared:
        return str(declared)
    import sys

    module = sys.modules.get(getattr(fn, "__module__", "") or "")
    version = getattr(module, "__version__", None) if module is not None else None
    name = f"{getattr(fn, '__module__', '') or ''}.{getattr(fn, '__qualname__', '') or ''}".strip(".")
    if version:
        return f"{name}=={version}"
    # No declared version: at least name the code that produced the row.
    return name or None


# --- probes -------------------------------------------------------------------


_IDENTITY_PROBE = """
SELECT i.indisvalid
  FROM pg_index i
  JOIN pg_class ic ON ic.oid = i.indexrelid
  JOIN pg_class t  ON t.oid = i.indrelid
  JOIN pg_namespace n ON n.oid = t.relnamespace
 WHERE n.nspname = 'silver' AND t.relname = 'signals'
   AND i.indisunique AND i.indpred IS NULL
   AND (SELECT array_agg(a.attname::text ORDER BY k.ord)
          FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum)
       = %s::text[]
"""


def has_identity_key(cur) -> bool:
    """True when silver.signals has a VALID unique index on the identity.

    An invalid index (a failed ``CREATE INDEX CONCURRENTLY`` leaves one) cannot
    be an ``ON CONFLICT`` arbiter, so it does not count.
    """
    try:
        cur.execute(_IDENTITY_PROBE, (list(IDENTITY_COLUMNS),))
        rows = cur.fetchall()
    except Exception:  # noqa: BLE001 - an unreadable catalogue means "not yet"
        return False
    return any(bool(r[0]) for r in rows)


def has_stamp_columns(cur) -> bool:
    """True when the stamp columns exist (the DDL of this release has run)."""
    try:
        cur.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = 'silver' AND table_name = 'signals' "
            "AND column_name = ANY(%s)",
            (list(STAMP_COLUMNS),),
        )
        got = cur.fetchone()
    except Exception:  # noqa: BLE001
        return False
    return bool(got) and int(got[0]) == len(STAMP_COLUMNS)


def _timescale_version(cur) -> tuple[int, ...] | None:
    cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
    got = cur.fetchone()
    if not got:
        return None
    parts = []
    for p in str(got[0]).split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _layout(cur) -> dict[str, Any]:
    """What the build path depends on: hypertable or not, compression, size."""
    from .timeseries import is_time_partitioned

    out: dict[str, Any] = {
        "timescaledb": None,
        "hypertable": False,
        "compression_enabled": False,
        "chunks": 0,
        "compressed_chunks": 0,
        "approximate_rows": None,
    }
    version = _timescale_version(cur)
    out["timescaledb"] = ".".join(str(p) for p in version) if version else None
    out["hypertable"] = is_time_partitioned(cur)
    if out["hypertable"]:
        cur.execute(
            "SELECT compression_enabled FROM timescaledb_information.hypertables "
            "WHERE hypertable_schema = 'silver' AND hypertable_name = 'signals'"
        )
        got = cur.fetchone()
        out["compression_enabled"] = bool(got and got[0])
        cur.execute(
            "SELECT count(*), count(*) FILTER (WHERE is_compressed) "
            "FROM timescaledb_information.chunks "
            "WHERE hypertable_schema = 'silver' AND hypertable_name = 'signals'"
        )
        got = cur.fetchone()
        out["chunks"], out["compressed_chunks"] = int(got[0]), int(got[1])
        cur.execute("SELECT approximate_row_count('silver.signals')")
        got = cur.fetchone()
        out["approximate_rows"] = int(got[0]) if got and got[0] is not None else None
    else:
        cur.execute("SELECT reltuples::bigint FROM pg_class WHERE oid = 'silver.signals'::regclass")
        got = cur.fetchone()
        out["approximate_rows"] = max(int(got[0]), 0) if got and got[0] is not None else None
    return out


def build_path(layout: Mapping[str, Any]) -> dict[str, str]:
    """How the unique index can be built on this layout, and what it locks.

    Measured, not assumed (see :data:`TIMESCALE_REFUSES_UNIQUE_ON_COMPRESSED`).
    """
    if not layout.get("hypertable"):
        return {
            "method": "concurrent",
            "statement": f"CREATE UNIQUE INDEX CONCURRENTLY {IDENTITY_INDEX} ON silver.signals "
            f"({', '.join(IDENTITY_COLUMNS)})",
            "locks": "SHARE UPDATE EXCLUSIVE: readers AND writers continue; two table scans.",
        }
    version = tuple(int(p) for p in str(layout.get("timescaledb") or "0").split("."))
    if layout.get("compression_enabled") and version < TIMESCALE_VERIFIED_UNIQUE_ON_COMPRESSED:
        return {
            "method": "upgrade-timescaledb-first",
            "statement": "ALTER EXTENSION timescaledb UPDATE",
            "locks": (
                f"TimescaleDB {layout.get('timescaledb')} refuses a unique index on a hypertable "
                "with compression enabled (measured on 2.17.2). Upgrade the extension "
                f"(verified on {'.'.join(map(str, TIMESCALE_VERIFIED_UNIQUE_ON_COMPRESSED))}), "
                "then build. Decompressing every chunk instead would multiply the table's "
                "size about sevenfold and is not the supported path."
            ),
        }
    return {
        "method": "hypertable-blocking-writes",
        "statement": f"CREATE UNIQUE INDEX {IDENTITY_INDEX} ON silver.signals "
        f"({', '.join(IDENTITY_COLUMNS)})",
        "locks": (
            "SHARE on the hypertable and every chunk for the length of the build: SELECTs "
            "(gold, charts, MCP) continue; INSERT/UPDATE/DELETE wait. Pause the conform "
            "timer first; its watermarks resume exactly where it stopped. TimescaleDB "
            "allows neither CONCURRENTLY nor transaction_per_chunk for a unique index."
        ),
    }


# --- preflight ------------------------------------------------------------------


def _scope_sql(
    site: str | None, start: Any, end: Any, alias: str = ""
) -> tuple[str, dict[str, Any]]:
    p = f"{alias}." if alias else ""
    clauses, params = [], {}
    if site:
        clauses.append(f"{p}site = %(site)s")
        params["site"] = site
    if start is not None:
        clauses.append(f"{p}ts >= %(start)s")
        params["start"] = start
    if end is not None:
        clauses.append(f"{p}ts < %(end)s")
        params["end"] = end
    return (" AND ".join(clauses) or "TRUE"), params


#: A content comparison the database can do on rows that predate the
#: ``content_hash`` column: every content column, NULL-safe.
_CONTENT_ROW = "ROW(" + ", ".join(CONTENT_COLUMNS) + ")::text"

_DUPLICATES_SQL = """
SELECT site, feed,
       count(*)                                     AS identities,
       sum(n - 1)                                   AS extra_rows,
       count(*) FILTER (WHERE contents = 1)         AS resend,
       count(*) FILTER (WHERE contents > 1)         AS divergent,
       max(n)                                       AS worst,
       min(ts)                                      AS first_ts,
       max(ts)                                      AS last_ts
FROM (
  SELECT site, feed, channel, ts, source_class,
         count(*) AS n,
         count(DISTINCT {content}) AS contents
  -- ADR-128 E5: a diagnostic read of the working tier. Only the table can
  -- say whether its own key would hold.
  FROM silver.signals
  WHERE {scope}
  GROUP BY site, feed, channel, ts, source_class
  HAVING count(*) > 1
) d
GROUP BY site, feed
ORDER BY divergent DESC, extra_rows DESC, site, feed
"""


def preflight(
    cur, *, site: str | None = None, start: Any = None, end: Any = None
) -> dict[str, Any]:
    """Read-only: what would violate the identity key, and the plan to fix it.

    Returns per ``(site, feed)``: identities carrying more than one row, the
    extra rows a collapse would remove, and how many of those identities are
    re-sends (every row holds the same content, so collapsing loses nothing)
    against divergent (the rows disagree, so the newest conform is kept and the
    rest go to the audit table). Plus the layout and the build path.

    The duplicate scan is a full GROUP BY over the scope. Scope it by ``site``
    and a window on a large node, the same way the operator runbook does.
    """
    scope, params = _scope_sql(site, start, end)
    try:
        cur.execute("SET TRANSACTION READ ONLY")
    except Exception:  # noqa: BLE001 - already inside a transaction that wrote; read anyway
        pass
    cur.execute(_DUPLICATES_SQL.format(content=_CONTENT_ROW, scope=scope), params)
    cols = [d[0] for d in cur.description]
    by_feed = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    for row in by_feed:
        for k in ("identities", "extra_rows", "resend", "divergent", "worst"):
            row[k] = int(row[k] or 0)
        for k in ("first_ts", "last_ts"):
            if isinstance(row.get(k), (datetime, date)):
                row[k] = row[k].isoformat()
    layout = _layout(cur)
    indexed = has_identity_key(cur)
    totals = {
        "identities": sum(r["identities"] for r in by_feed),
        "extra_rows": sum(r["extra_rows"] for r in by_feed),
        "resend": sum(r["resend"] for r in by_feed),
        "divergent": sum(r["divergent"] for r in by_feed),
    }
    return {
        "tier_read": "silver",
        "scope": {"site": site, "start": _iso(start), "end": _iso(end)},
        "identity": list(IDENTITY_COLUMNS),
        "index_present": indexed,
        "duplicates": by_feed,
        "totals": totals,
        "layout": layout,
        "build": build_path(layout),
        "plan": _plan(totals, layout, indexed),
    }


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, (datetime, date)) else value


def _plan(totals: Mapping[str, int], layout: Mapping[str, Any], indexed: bool) -> list[str]:
    if indexed:
        return ["the identity index is present and valid; conform already upserts on it"]
    steps: list[str] = []
    if totals["extra_rows"]:
        steps.append(
            f"collapse {totals['extra_rows']:,} extra row(s) over {totals['identities']:,} "
            "identities: keep the newest conform (conformed_at, then row_hash, which is the "
            f"row gold.signals_latest already serves), copy the rest to {AUDIT_TABLE}"
        )
        if totals["divergent"]:
            steps.append(
                f"{totals['divergent']:,} of those identities are DIVERGENT (the rows disagree). "
                "collapse refuses them unless --accept-divergent is given; the losers stay "
                f"recoverable in {AUDIT_TABLE}"
            )
    else:
        steps.append("no duplicates in scope: nothing to collapse")
    path = build_path(layout)
    steps.append(f"build the index ({path['method']}): {path['statement']}")
    steps.append(f"lock profile: {path['locks']}")
    steps.append("re-run preflight: index_present must be true; conform switches by itself")
    return steps


# --- collapse -------------------------------------------------------------------


_AUDIT_DDL = [
    f"CREATE TABLE IF NOT EXISTS {AUDIT_TABLE} (LIKE silver.signals INCLUDING DEFAULTS)",
    f"ALTER TABLE {AUDIT_TABLE} ADD COLUMN IF NOT EXISTS kept_row_hash text",
    f"ALTER TABLE {AUDIT_TABLE} ADD COLUMN IF NOT EXISTS collapse_reason text",
    f"ALTER TABLE {AUDIT_TABLE} ADD COLUMN IF NOT EXISTS collapsed_at timestamptz NOT NULL DEFAULT now()",
    f"COMMENT ON TABLE {AUDIT_TABLE} IS "
    "'Rows removed when silver.signals took the reading-identity key (ADR-206); "
    "kept_row_hash names the row that stayed'",
]

#: Rank every row of every duplicated identity; rank 1 stays.
#:
#: "Newest conform" is ``conformed_at``, which rows written before this release
#: do not have. Among those the tie breaks on ``row_hash DESC``, which is
#: exactly the row ``gold.signals_latest`` serves today, so collapsing changes
#: no answer a reader is already getting.
_RANKED = """
WITH dup AS (
  SELECT site, feed, channel, ts, source_class,
         count(DISTINCT {content}) AS contents
    FROM silver.signals
   WHERE {scope}
   GROUP BY site, feed, channel, ts, source_class
  HAVING count(*) > 1
), ranked AS (
  SELECT s.row_hash, s.channel, s.ts, d.contents,
         row_number() OVER w AS rn,
         first_value(s.row_hash) OVER w AS kept_row_hash
    FROM silver.signals s
    JOIN dup d USING (site, feed, channel, ts, source_class)
   WHERE {scope_s}
  WINDOW w AS (PARTITION BY s.site, s.feed, s.channel, s.ts, s.source_class
               ORDER BY s.conformed_at DESC NULLS LAST, s.row_hash DESC)
)
"""


def _windows(cur, site: str | None, start: Any, end: Any, days: int):
    """Day-wide windows over the scope, so each commit is bounded."""
    scope, params = _scope_sql(site, start, end)
    cur.execute(f"SELECT min(ts), max(ts) FROM silver.signals WHERE {scope}", params)
    lo, hi = cur.fetchone()
    if lo is None:
        return
    lo = max(lo, start) if start is not None else lo
    step = timedelta(days=max(1, days))
    at = datetime(lo.year, lo.month, lo.day, tzinfo=lo.tzinfo)
    stop = end if end is not None else hi + timedelta(microseconds=1)
    while at < stop:
        yield at, min(at + step, stop)
        at += step


def collapse(
    conn,
    *,
    site: str | None = None,
    start: Any = None,
    end: Any = None,
    apply: bool = False,
    accept_divergent: bool = False,
    window_days: int = 1,
) -> dict[str, Any]:
    """Remove every row that would violate the identity key; keep a copy of each.

    One window (``window_days``, aligned to chunks on a day-chunked hypertable)
    per transaction, so no transaction holds row locks over the whole table and
    a run cut short resumes with nothing half-done: each window copies its
    losers to :data:`AUDIT_TABLE` and deletes them in the same transaction.

    Divergent identities (the rows disagree) are left alone unless
    ``accept_divergent``; they are counted so the operator sees what a
    decision is needed for. ``apply=False`` computes every count in a
    transaction it rolls back.
    """
    report: dict[str, Any] = {
        "applied": bool(apply),
        "windows": 0,
        "removed": 0,
        "removed_resend": 0,
        "removed_divergent": 0,
        "divergent_left": 0,
        "audit_table": AUDIT_TABLE,
    }
    with conn.cursor() as cur:
        for stmt in _AUDIT_DDL:
            cur.execute(stmt)
        if apply:
            conn.commit()
        windows = list(_windows(cur, site, start, end, window_days))
        for lo, hi in windows:
            report["windows"] += 1
            scope, params = _scope_sql(site, lo, hi)
            scope_s, _ = _scope_sql(site, lo, hi, alias="s")
            ranked = _RANKED.format(content=_CONTENT_ROW, scope=scope, scope_s=scope_s)
            cur.execute(
                ranked + "SELECT count(*) FILTER (WHERE rn > 1 AND contents = 1), "
                "count(*) FILTER (WHERE rn > 1 AND contents > 1), "
                "count(DISTINCT (channel, ts)) FILTER (WHERE contents > 1) FROM ranked",
                params,
            )
            resend, divergent, divergent_ids = (int(x or 0) for x in cur.fetchone())
            allowed = "TRUE" if accept_divergent else "contents = 1"
            if not accept_divergent:
                report["divergent_left"] += divergent_ids
            if not (resend or (divergent and accept_divergent)):
                continue
            cols = _signal_columns(cur)
            col_list = ", ".join(cols)
            s_cols = ", ".join(f"s.{c}" for c in cols)
            cur.execute(
                ranked
                + f"""INSERT INTO {AUDIT_TABLE} ({col_list}, kept_row_hash, collapse_reason)
                     SELECT {s_cols}, r.kept_row_hash,
                            CASE WHEN r.contents = 1 THEN 'resend' ELSE 'divergent' END
                       FROM silver.signals s
                       JOIN ranked r ON r.row_hash = s.row_hash AND r.channel = s.channel AND r.ts = s.ts
                      WHERE r.rn > 1 AND {allowed.replace('contents', 'r.contents')} AND {scope_s}""",
                params,
            )
            cur.execute(
                ranked
                # ADR-128 E4: silver's own migration onto its reading-identity key (ADR-206).
                + f"""DELETE FROM silver.signals s USING ranked r
                      WHERE r.row_hash = s.row_hash AND r.channel = s.channel AND r.ts = s.ts
                        AND r.rn > 1 AND {allowed.replace('contents', 'r.contents')} AND {scope_s}""",
                params,
            )
            removed = cur.rowcount
            report["removed"] += removed
            report["removed_resend"] += resend
            if accept_divergent:
                report["removed_divergent"] += divergent
            if apply:
                conn.commit()
        if not apply:
            conn.rollback()
    return report


def _signal_columns(cur) -> list[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'silver' AND table_name = 'signals' ORDER BY ordinal_position"
    )
    return [r[0] for r in cur.fetchall()]


# --- build ----------------------------------------------------------------------


def build_index(conn, *, lock_timeout_s: float = 10.0) -> dict[str, Any]:
    """Build the identity index by the path :func:`build_path` names.

    ``conn`` must be in autocommit (CONCURRENTLY cannot run in a transaction,
    and the hypertable build should not sit inside a caller's). A
    ``lock_timeout`` makes it fail fast rather than queue behind a running
    conform pass. Refuses, without touching anything, when duplicates remain
    or the TimescaleDB version cannot build it.
    """
    with conn.cursor() as cur:
        if has_identity_key(cur):
            return {"built": False, "reason": "already present and valid", "index": IDENTITY_INDEX}
        layout = _layout(cur)
        path = build_path(layout)
        if path["method"] == "upgrade-timescaledb-first":
            return {"built": False, "reason": path["locks"], "index": IDENTITY_INDEX, "layout": layout}
        # An invalid leftover of an interrupted CONCURRENTLY build blocks a new one.
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE schemaname = 'silver' AND indexname = %s",
            (IDENTITY_INDEX,),
        )
        if cur.fetchone() is not None:
            cur.execute(f"DROP INDEX {'CONCURRENTLY ' if path['method'] == 'concurrent' else ''}"
                        f"silver.{IDENTITY_INDEX}")
        cur.execute(f"SET lock_timeout = '{int(lock_timeout_s * 1000)}ms'")
        try:
            cur.execute(path["statement"])
        finally:
            cur.execute("RESET lock_timeout")
        return {
            "built": has_identity_key(cur),
            "method": path["method"],
            "locks": path["locks"],
            "index": IDENTITY_INDEX,
            "layout": layout,
        }


#: The DDL statement that gives a FRESH install the key with no migration.
#:
#: Only when the table is empty: on a populated node a deploy must never build
#: a unique index (a full scan, a write lock on a hypertable, and a failed
#: deploy if one duplicate exists). Those nodes take the operator path. A
#: TimescaleDB that refuses the index (an empty hypertable with compression on
#: an old version) leaves a NOTICE rather than failing the deploy, and the
#: writer keeps the legacy key until the index exists.
IDENTITY_INDEX_DDL = f"""DO $$ BEGIN
     IF NOT EXISTS (SELECT 1 FROM pg_indexes
                     WHERE schemaname = 'silver' AND indexname = '{IDENTITY_INDEX}')
        AND NOT EXISTS (SELECT 1 FROM silver.signals LIMIT 1)
     THEN
       BEGIN
         CREATE UNIQUE INDEX {IDENTITY_INDEX}
           ON silver.signals ({", ".join(IDENTITY_COLUMNS)});
       EXCEPTION WHEN feature_not_supported THEN
         RAISE NOTICE 'reading identity index not built here: %', SQLERRM;
       END;
     END IF;
   END $$"""

# contract: once every node reports index_present (axi data reading-identity
# preflight), drop the transport key: ALTER TABLE silver.signals DROP CONSTRAINT
# signals_pkey, re-point silver.signal_uncertainty at the identity, and remove
# the legacy ON CONFLICT (row_hash, ...) branch from conformance._bind_signals.

__all__ = [
    "AUDIT_TABLE",
    "CONTENT_COLUMNS",
    "IDENTITY_COLUMNS",
    "IDENTITY_INDEX",
    "IDENTITY_INDEX_DDL",
    "REPLACE_COLUMNS",
    "STAMP_COLUMNS",
    "build_index",
    "build_path",
    "collapse",
    "content_hash",
    "has_identity_key",
    "has_stamp_columns",
    "normalizer_version",
    "preflight",
]
