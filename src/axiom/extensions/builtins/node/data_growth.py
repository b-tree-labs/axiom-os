# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""KEEL's data-growth report: which tables on this node's database grow without a bound.

A database rarely fails by filling in a day. It fails by a table that grows
forever with nobody having decided how much of it to keep, by indexes larger
than the data they index, and by a row that is rewritten thousands of times.
This module measures those, per table, from the catalogue only, and judges
them against one rule:

    A table that grows with time is a compressed hypertable, or it declares a
    row limit or a retention.

**Metadata only.** Sizes come from ``pg_class`` and the size functions, row
and write counts from ``pg_stat_user_tables``, index shapes from ``pg_index``
and ``pg_stat_user_indexes``, hypertable facts from
``timescaledb_information.*``. Nothing here reads a table's rows: no
``count(*)``, no ``min``/``max`` over data. A report over a 200 GB table costs
the same as one over an empty database.

**Growth needs history.** The per-table rate comes from repeated samples KEEL
keeps in its own small SQLite table under the node's state directory (never in
the database being measured, so the report opens a read-only session), or, for
a hypertable, from its chunk times when no history exists yet.

**It never acts.** Each finding carries its fix as a plan: the capability that
applies it (when one exists), its parameters and the ``plan_hash`` an approval
binds to, or the SQL a person runs inside a maintenance window.

Where tables declare their bound: PLINTH owns the declarations
(``data_platform.timeseries`` and ``data_platform.table_limits`` in
``data_platform.toml``) and returns them, with the backup posture, from
``data.storage_rules``. KEEL reads them through the registry, never by importing
PLINTH's code. A table the nightly dump leaves out and that is not declared
``rederivable`` is a finding too.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

GB = 2**30
DAY = 86400.0

#: Tables smaller than this are reported but not judged: a rule broken by a
#: 40 MB table is noise in a daily alert.
DEFAULT_MIN_BYTES = 1 * GB
#: Updates per insert above which a table is churning (a row rewritten in place).
DEFAULT_CHURN_RATIO = 20.0
#: ...and only once the updates are numerous enough to matter.
DEFAULT_CHURN_MIN_UPDATES = 1_000_000
#: Alert when the database volume is this many days from full, or closer.
DEFAULT_RUNWAY_DAYS = 60.0
#: Samples older than this are not used for a rate.
DEFAULT_WINDOW_DAYS = 14.0
#: Two samples closer than this give no rate (noise dominates).
MIN_SAMPLE_SPAN_S = 12 * 3600.0
#: Samples kept per table.
KEEP_SAMPLES_DAYS = 90

INTERNAL_SCHEMAS = (
    "pg_catalog",
    "information_schema",
    "pg_toast",
    "_timescaledb_internal",
    "_timescaledb_catalog",
    "_timescaledb_config",
    "_timescaledb_cache",
    "_timescaledb_functions",
    "timescaledb_information",
    "timescaledb_experimental",
)


# -- what the catalogue says ---------------------------------------------------------


@dataclass
class TableStat:
    schema: str
    name: str
    total_bytes: int = 0
    table_bytes: int = 0
    index_bytes: int = 0
    live_rows: int = 0
    dead_rows: int = 0
    inserts: int = 0
    updates: int = 0
    deletes: int = 0
    hot_updates: int = 0
    hypertable: bool = False
    compression: bool = False
    compress_after: str | None = None
    retention: str | None = None
    #: Bytes per day from the hypertable's chunks, when the catalogue can say.
    chunk_bytes_per_day: float | None = None

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class IndexStat:
    schema: str
    table: str
    name: str
    columns: tuple[str, ...]
    unique: bool = False
    primary: bool = False
    bytes: int = 0
    scans: int | None = None

    @property
    def qualified_table(self) -> str:
        return f"{self.schema}.{self.table}"


class Catalog(Protocol):
    """What the report needs from a database: the catalogue, never the rows.
    :class:`PostgresCatalog` is the real one; tests pass a fake."""

    def tables(self) -> list[TableStat]: ...
    def indexes(self) -> list[IndexStat]: ...
    def database_bytes(self) -> int | None: ...
    def data_directory(self) -> str | None: ...
    def stats_reset(self) -> str | None: ...


def _interval(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, timedelta):
        days = v.total_seconds() / DAY
        return f"{days:g}d" if days >= 1 else f"{v.total_seconds() / 3600:g}h"
    return str(v)


class PostgresCatalog:
    """The catalogue of a PostgreSQL (and, when present, TimescaleDB) database.

    Takes a DB-API cursor. Every query is over catalogue views and size
    functions; :data:`QUERIES` lists them so a test can prove none reads rows.
    """

    TABLES = (
        "SELECT n.nspname, c.relname, c.relkind, pg_total_relation_size(c.oid), "
        "pg_table_size(c.oid), pg_indexes_size(c.oid), "
        "coalesce(s.n_live_tup, 0), coalesce(s.n_dead_tup, 0), coalesce(s.n_tup_ins, 0), "
        "coalesce(s.n_tup_upd, 0), coalesce(s.n_tup_del, 0), coalesce(s.n_tup_hot_upd, 0) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid "
        "WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition AND n.nspname <> ALL(%s)"
    )
    INDEXES = (
        "SELECT n.nspname, t.relname, i.relname, ix.indisunique, ix.indisprimary, "
        "pg_relation_size(i.oid), su.idx_scan, "
        "ARRAY(SELECT coalesce(a.attname, '(expression)') "
        "      FROM unnest(ix.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) "
        "      LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
        "      ORDER BY k.ord) "
        "FROM pg_index ix JOIN pg_class i ON i.oid = ix.indexrelid "
        "JOIN pg_class t ON t.oid = ix.indrelid JOIN pg_namespace n ON n.oid = t.relnamespace "
        "LEFT JOIN pg_stat_user_indexes su ON su.indexrelid = ix.indexrelid "
        "WHERE t.relkind IN ('r', 'p') AND n.nspname <> ALL(%s)"
    )
    HAS_TIMESCALE = "SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"
    HYPERTABLES = (
        "SELECT hypertable_schema, hypertable_name, compression_enabled "
        "FROM timescaledb_information.hypertables"
    )
    DB_BYTES = "SELECT pg_database_size(current_database())"
    DATA_DIR = "SHOW data_directory"
    STATS_RESET = "SELECT stats_reset FROM pg_stat_database WHERE datname = current_database()"

    def __init__(self, cur: Any) -> None:
        self.cur = cur

    @classmethod
    def queries(cls) -> tuple[str, ...]:
        return tuple(v for k, v in vars(cls).items() if k.isupper() and isinstance(v, str))

    def _timescale(self) -> bool:
        self.cur.execute(self.HAS_TIMESCALE)
        return self.cur.fetchone() is not None

    HYPER_SIZE = (
        "SELECT table_bytes, index_bytes, total_bytes FROM hypertable_detailed_size(%s::regclass)"
    )
    HYPER_POLICIES = (
        "SELECT proc_name, config->>'compress_after', config->>'drop_after' "
        "FROM timescaledb_information.jobs WHERE hypertable_schema = %s AND hypertable_name = %s "
        "AND proc_name IN ('policy_compression', 'policy_retention')"
    )
    #: Bytes of the chunks whose time range ended in the last week, per day: the chunk
    #: catalogue and its size function, no rows.
    HYPER_RATE = (
        "SELECT sum(ds.total_bytes) / 7.0 "
        "FROM timescaledb_information.chunks c "
        "JOIN chunks_detailed_size(%s::regclass) ds USING (chunk_schema, chunk_name) "
        "WHERE c.hypertable_schema = %s AND c.hypertable_name = %s "
        "AND c.range_end > now() - interval '7 days' AND c.range_end <= now() "
    )
    PARTITION_SIZE = (
        "SELECT coalesce(sum(pg_table_size(p.relid)), 0), coalesce(sum(pg_indexes_size(p.relid)), 0), "
        "coalesce(sum(pg_total_relation_size(p.relid)), 0) "
        "FROM pg_partition_tree(%s::regclass) p WHERE p.isleaf"
    )

    def tables(self) -> list[TableStat]:
        self.cur.execute(self.TABLES, (list(INTERNAL_SCHEMAS),))
        rows = self.cur.fetchall()
        hyper: dict[str, bool] = {}
        if self._timescale():
            self.cur.execute(self.HYPERTABLES)
            hyper = {f"{s}.{t}": bool(c) for s, t, c in self.cur.fetchall()}
        out: list[TableStat] = []
        for schema, name, relkind, total, tb, ib, live, dead, ins, upd, dele, hot in rows:
            t = TableStat(
                schema=schema, name=name, total_bytes=int(total or 0), table_bytes=int(tb or 0),
                index_bytes=int(ib or 0), live_rows=int(live or 0), dead_rows=int(dead or 0),
                inserts=int(ins or 0), updates=int(upd or 0), deletes=int(dele or 0),
                hot_updates=int(hot or 0),
            )
            if t.qualified in hyper:
                self._hypertable(t, hyper[t.qualified])
            elif relkind == "p":
                self.cur.execute(self.PARTITION_SIZE, (t.qualified,))
                tb2, ib2, tot2 = self.cur.fetchone()
                t.table_bytes, t.index_bytes, t.total_bytes = int(tb2), int(ib2), int(tot2)
            out.append(t)
        return out

    def _hypertable(self, t: TableStat, compression: bool) -> None:
        t.hypertable, t.compression = True, compression
        self.cur.execute(self.HYPER_SIZE, (t.qualified,))
        row = self.cur.fetchone()
        if row:
            t.table_bytes, t.index_bytes, t.total_bytes = (int(x or 0) for x in row)
        self.cur.execute(self.HYPER_POLICIES, (t.schema, t.name))
        for proc, after, drop in self.cur.fetchall():
            if proc == "policy_compression":
                t.compress_after = after
            else:
                t.retention = drop
        self.cur.execute(self.HYPER_RATE, (t.qualified, t.schema, t.name))
        row = self.cur.fetchone()
        t.chunk_bytes_per_day = float(row[0]) if row and row[0] is not None else None

    def indexes(self) -> list[IndexStat]:
        self.cur.execute(self.INDEXES, (list(INTERNAL_SCHEMAS),))
        return [
            IndexStat(schema=s, table=t, name=i, unique=bool(u), primary=bool(p),
                      bytes=int(b or 0), scans=None if sc is None else int(sc),
                      columns=tuple(cols or ()))
            for s, t, i, u, p, b, sc, cols in self.cur.fetchall()
        ]

    def _one(self, sql: str) -> Any:
        try:
            self.cur.execute(sql)
            row = self.cur.fetchone()
        except Exception:  # noqa: BLE001 - a setting the role may not read
            return None
        return row[0] if row else None

    def database_bytes(self) -> int | None:
        v = self._one(self.DB_BYTES)
        return int(v) if v is not None else None

    def data_directory(self) -> str | None:
        return self._one(self.DATA_DIR)

    def stats_reset(self) -> str | None:
        v = self._one(self.STATS_RESET)
        return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)


# -- KEEL's own history -------------------------------------------------------------


class SampleStore:
    """Sizes over time, in KEEL's own small JSON file under the state directory.

    Kept outside the measured database on purpose: the report then needs no
    write access to it, and the history survives a database restore.
    """

    FILE = Path("node") / "data_growth_samples.json"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def under(cls, state_dir: Path | str) -> SampleStore:
        return cls(Path(state_dir) / cls.FILE)

    def _load(self) -> dict[str, dict[str, int]]:
        """``{relation: {at: bytes}}`` (``at`` as the float's repr, a JSON key)."""
        try:
            doc = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return doc if isinstance(doc, dict) else {}

    def record(self, at: float, sizes: dict[str, int]) -> None:
        from axiom.infra.state_integrity import atomic_write_text

        doc = self._load()
        for rel, b in sizes.items():
            doc.setdefault(rel, {})[repr(float(at))] = int(b)
        cutoff = at - KEEP_SAMPLES_DAYS * DAY
        doc = {
            rel: kept
            for rel, series in doc.items()
            if (kept := {k: v for k, v in series.items() if float(k) >= cutoff})
        }
        atomic_write_text(self.path, json.dumps(doc, sort_keys=True))

    def rate(self, relation: str, now: float, current: int,
             window_days: float = DEFAULT_WINDOW_DAYS) -> float | None:
        """Bytes per day from the oldest sample in the window to ``current``;
        ``None`` until the history spans at least twelve hours."""
        inside = sorted(
            (float(k), v) for k, v in self._load().get(relation, {}).items()
            if now - window_days * DAY <= float(k) < now
        )
        if not inside or now - inside[0][0] < MIN_SAMPLE_SPAN_S:
            return None
        at, b = inside[0]
        return (current - b) / ((now - at) / DAY)


# -- declarations -------------------------------------------------------------------


@dataclass
class Limit:
    """How a table declares its bound (or that it does not grow with time), and whether
    its rows can be re-derived from their source."""

    qualified: str
    max_rows: int | None = None
    retain: str | None = None
    grows: bool = True
    rederivable: bool = False
    source: str = "data_platform.table_limits"

    @property
    def bounded(self) -> bool:
        return self.max_rows is not None or bool(self.retain)


def limits_from_entries(entries: Any) -> dict[str, Limit]:
    """Parse ``data_platform.table_limits`` entries. A malformed entry is skipped."""
    if isinstance(entries, dict):
        entries = [entries]
    out: dict[str, Limit] = {}
    for e in entries or []:
        if not isinstance(e, dict) or not e.get("table"):
            continue
        q = f"{e.get('schema') or 'public'}.{e['table']}"
        try:
            mr = e.get("max_rows")
            out[q] = Limit(
                qualified=q, max_rows=int(mr) if mr is not None else None,
                retain=str(e["retain"]) if e.get("retain") else None,
                grows=bool(e.get("grows", True)),
                rederivable=bool(e.get("rederivable", False)),
            )
        except (TypeError, ValueError):
            continue
    return out


def limits_from_rules(rules: Iterable[dict[str, Any]]) -> dict[str, Limit]:
    """The rules PLINTH's ``data.storage_rules`` returns, as KEEL judges them. KEEL reads the
    declarations through the registry, never by importing PLINTH's config code."""
    out: dict[str, Limit] = {}
    for r in rules or []:
        q = r.get("table")
        if not q:
            continue
        out[q] = Limit(
            qualified=q, max_rows=r.get("max_rows"), retain=r.get("retain"),
            grows=bool(r.get("grows", True)), rederivable=bool(r.get("rederivable", False)),
            source=str(r.get("source") or ""),
        )
    return out


def backup_state(qualified: str, backup: dict[str, Any] | None) -> str:
    """``dumped``, ``excluded`` (data left out of the nightly dump), ``no backup`` (no
    enabled policy), or ``unknown`` (the posture was not readable)."""
    if not backup or "enabled" not in backup:
        return "unknown"
    if not backup.get("enabled"):
        return "no backup"
    schemas = backup.get("schemas")
    if schemas and qualified.split(".", 1)[0] not in schemas:
        return "excluded"
    if any(fnmatch.fnmatchcase(qualified, pat) for pat in backup.get("exclude_table_data") or []):
        return "excluded"
    return "dumped"


# -- the rules ----------------------------------------------------------------------


@dataclass
class Thresholds:
    min_bytes: int = DEFAULT_MIN_BYTES
    churn_ratio: float = DEFAULT_CHURN_RATIO
    churn_min_updates: int = DEFAULT_CHURN_MIN_UPDATES
    runway_days: float = DEFAULT_RUNWAY_DAYS


def plan(capability: str | None, params: dict[str, Any], steps: list[str],
         sql: list[str] | None = None) -> dict[str, Any]:
    """A proposed fix, bound to the capability that applies it.

    ``plan_hash`` is the digest that capability's approval binds to: calling the capability
    with exactly ``params`` and no ``plan_hash`` opens the plan for a person to approve, and
    the call with the approved hash runs it. A fix whose steps are statements binds to
    ``node.data_apply`` with ``{table, sql}``. A fix that is only advice (a writer to change,
    a declaration to add) has no capability and its hash only identifies it."""
    from axiom.infra.skill_chain import plan_digest

    if sql and capability is None:
        capability = APPLY
        params = {"table": params.get("table"), "sql": list(sql)}
    return {
        "capability": capability,
        "params": dict(params),
        "steps": list(steps),
        "sql": list(sql or []),
        "plan_hash": plan_digest(capability or "advice", {**params, "steps": list(steps)}
                                 if capability is None else params),
        "applies": "only with a person's approval of this plan_hash; KEEL never acts on its own",
    }


#: KEEL's one capability that changes the database: runs an approved plan's statements.
APPLY = "node.data_apply"

#: The statements an approved plan may carry. Anything else is refused, approved or not.
ALLOWED_STATEMENTS = (
    r"SELECT add_retention_policy\('[\w.]+', INTERVAL '[\w ]+'\)",
    r"SELECT add_compression_policy\('[\w.]+', INTERVAL '[\w ]+'\)",
    r"DROP INDEX CONCURRENTLY [\w.]+",
    r"ALTER TABLE [\w.]+ SET \(fillfactor = \d{2}\)",
)


def allowed(statement: str) -> bool:
    import re

    return any(re.fullmatch(p, statement.strip()) for p in ALLOWED_STATEMENTS)


def retention_plans(rules: list[dict[str, Any]], tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retention and archival for tables that DECLARE a bound: what each needs, as plans."""
    by = {t["table"]: t for t in tables}
    out: list[dict[str, Any]] = []
    for r in rules or []:
        q = r.get("table")
        t = by.get(q)
        if t is None or not (r.get("retain") or r.get("max_rows") is not None):
            continue
        steps: list[str] = []
        sql: list[str] = []
        state = "kept"
        if r.get("retain"):
            if t.get("hypertable"):
                if t.get("retention"):
                    steps.append(f"kept by its retention policy (drops chunks after {t['retention']})")
                else:
                    state = "needs a policy"
                    sql.append(f"SELECT add_retention_policy('{q}', INTERVAL '{r['retain']}')")
                    steps.append("add the retention policy the declaration asks for; whole "
                                 "chunks are dropped, with no row deletes")
            else:
                state = "needs a job"
                steps.append(f"delete rows older than {r['retain']} in batches by the table's "
                             "time column inside a maintenance window, archiving them first "
                             "when the runbook asks; or adopt it as a hypertable so retention "
                             "drops chunks instead")
        if r.get("max_rows") is not None and (t.get("live_rows") or 0) > r["max_rows"]:
            state = "over its limit"
            steps.append(f"trim the oldest rows down to {r['max_rows']:,} "
                         f"(it holds about {t['live_rows']:,})")
        out.append({
            "table": q, "state": state, "retain": r.get("retain"), "max_rows": r.get("max_rows"),
            "fix": plan(None, {"table": q}, steps, sql=sql) if state != "kept" else None,
        })
    return out


def why(report: dict[str, Any], top: int = 5) -> str:
    """The deterministic answer to "why is the database growing"."""
    tables = report.get("tables") or []
    growing = sorted((t for t in tables if (t.get("bytes_per_day") or 0) > 0),
                     key=lambda t: t["bytes_per_day"], reverse=True)[:top]
    largest = sorted(tables, key=lambda t: t.get("total_bytes") or 0, reverse=True)[:top]
    lines: list[str] = []
    if growing:
        lines.append("Growing fastest: " + "; ".join(
            f"{t['table']} about {_gb(t['bytes_per_day'])}/day" for t in growing))
    else:
        lines.append("No growth rate yet: KEEL needs a second daily sample (or hypertable "
                     "chunks) to measure one.")
    if largest:
        lines.append("Largest: " + "; ".join(
            f"{t['table']} {_gb(t['total_bytes'])} (indexes {_gb(t['index_bytes'])})"
            for t in largest))
    causes: dict[str, list[str]] = {}
    for f in report.get("findings") or []:
        causes.setdefault(f["code"], []).append(f["table"])
    for code, tabs in causes.items():
        lines.append(f"{code.replace('-', ' ')}: {', '.join(tabs)}")
    vol = report.get("volume") or {}
    if vol.get("days_to_full") is not None:
        lines.append(f"The database volume has {_gb(vol['free_bytes'])} free, about "
                     f"{vol['days_to_full']:.0f} days at this rate.")
    return "\n".join(lines)


@dataclass
class Finding:
    code: str
    severity: str
    table: str
    message: str
    fix: dict[str, Any] = field(default_factory=dict)


def _gb(n: float | int | None) -> str:
    return "?" if n is None else f"{n / GB:.1f} GB"


def _grows(t: TableStat, rate: float | None, limit: Limit | None) -> bool:
    if limit is not None and not limit.grows:
        return False
    if rate is not None:
        return rate > 0
    # No history yet: a table that takes far more inserts than deletes is growing.
    return t.inserts > 2 * t.deletes


def judge(tables: Iterable[TableStat], indexes: Iterable[IndexStat], limits: dict[str, Limit],
          rates: dict[str, float | None], th: Thresholds,
          backup: dict[str, Any] | None = None) -> list[Finding]:
    out: list[Finding] = []
    by_table: dict[str, list[IndexStat]] = {}
    for ix in indexes:
        by_table.setdefault(ix.qualified_table, []).append(ix)
    for t in tables:
        limit = limits.get(t.qualified)
        if limit and limit.max_rows is not None and t.live_rows > limit.max_rows:
            out.append(Finding(
                "over-row-limit", "warning", t.qualified,
                f"{t.qualified} holds about {t.live_rows:,} rows, over its declared limit of "
                f"{limit.max_rows:,}",
                plan(None, {"table": t.qualified}, [
                    "the job that trims this table to its limit is not running or not keeping up; "
                    "find it and run it, or raise the declared max_rows if the limit is wrong",
                ]),
            ))
        if t.total_bytes < th.min_bytes:
            continue
        state = backup_state(t.qualified, backup)
        if state in ("excluded", "no backup") and not (limit and limit.rederivable):
            why = ("its data is left out of the nightly dump" if state == "excluded"
                   else "no backup policy is enabled")
            out.append(Finding(
                "not-backed-up", "warning", t.qualified,
                f"{t.qualified} ({_gb(t.total_bytes)}) is not backed up ({why}) and is not "
                "declared re-derivable: losing the volume loses it",
                plan(None, {"table": t.qualified}, [
                    "if its rows can be rebuilt from their source, declare rederivable = true "
                    "for it in data_platform.table_limits (and say how in the runbook)",
                    "otherwise take it out of the backup policy's exclude_table_data, or enable "
                    "the backup policy (`axi data backup-policy`)",
                ]),
            ))
        rate = rates.get(t.qualified)
        bounded = (
            (t.hypertable and t.compression and bool(t.compress_after))
            or bool(t.retention)
            or bool(limit and limit.bounded)
        )
        if not bounded and _grows(t, rate, limit):
            per_day = f", about {_gb(rate)}/day" if rate else ""
            if t.hypertable:
                why = "a hypertable with no compression policy and no retention"
                fix = plan("data.ensure_schema", {"table": t.qualified}, [
                    f"declare {t.qualified} in data_platform.timeseries with engine = "
                    "\"hypertable\" and compress_after (and retain, if old data may go)",
                    "apply it with data.ensure_schema, which adds the compression policy",
                ])
            else:
                why = "a plain table with no compression, no row limit and no retention"
                fix = plan("data.adopt_timeseries", {"table": t.qualified}, [
                    f"declare {t.qualified} in data_platform.timeseries with engine = "
                    "\"hypertable\", its time column and compress_after",
                    "convert it in place with data.adopt_timeseries inside a maintenance window",
                    "or, if it should not keep everything, declare max_rows or retain for it in "
                    "data_platform.table_limits, or grows = false if it does not grow with time",
                ])
            out.append(Finding(
                "unbounded-growth", "warning", t.qualified,
                f"{t.qualified} is {_gb(t.total_bytes)} and growing{per_day}: {why}", fix,
            ))
        if (
            t.updates >= th.churn_min_updates
            and t.updates / max(t.inserts, 1) >= th.churn_ratio
        ):
            hot_pct = 100.0 * t.hot_updates / t.updates if t.updates else 0.0
            out.append(Finding(
                "update-churn", "warning", t.qualified,
                f"{t.qualified} has taken {t.updates:,} updates for {t.inserts:,} inserts "
                f"({t.updates / max(t.inserts, 1):,.0f} per insert, {hot_pct:.0f}% in place); "
                f"{t.dead_rows:,} dead rows",
                plan(None, {"table": t.qualified}, [
                    "find the writer that rewrites the same rows: record a state change once, "
                    "or update only when a value changed",
                    "leave room for in-place updates so they avoid index writes",
                ], sql=[f"ALTER TABLE {t.qualified} SET (fillfactor = 80)"]),
            ))
        if t.table_bytes and t.index_bytes > t.table_bytes:
            unused = [ix for ix in by_table.get(t.qualified, [])
                      if ix.scans == 0 and not ix.unique and not ix.primary]
            out.append(Finding(
                "index-heavy", "info", t.qualified,
                f"{t.qualified}'s indexes take {_gb(t.index_bytes)}, more than its "
                f"{_gb(t.table_bytes)} of data",
                plan(None, {"table": t.qualified}, [
                    "drop indexes no query uses (listed in sql, never scanned since the last "
                    "statistics reset), and any an overlapping-indexes finding names",
                ], sql=[f"DROP INDEX CONCURRENTLY {ix.schema}.{ix.name}" for ix in unused]),
            ))
        out.extend(_overlaps(t.qualified, by_table.get(t.qualified, [])))
    return out


def _overlaps(table: str, idx: list[IndexStat]) -> list[Finding]:
    out: list[Finding] = []
    seen: set[frozenset[str]] = set()
    for a in idx:
        for b in idx:
            if a.name >= b.name or not a.columns or not b.columns:
                continue
            if a.columns[0] != b.columns[0] or "(expression)" in a.columns[:1]:
                continue
            pair = frozenset((a.name, b.name))
            if pair in seen:
                continue
            seen.add(pair)
            shorter, longer = (a, b) if len(a.columns) <= len(b.columns) else (b, a)
            redundant = (
                longer.columns[: len(shorter.columns)] == shorter.columns
                and not shorter.unique and not shorter.primary
            )
            sql = [f"DROP INDEX CONCURRENTLY {shorter.schema}.{shorter.name}"] if redundant else []
            steps = (
                [f"{shorter.name} ({', '.join(shorter.columns)}) is a prefix of {longer.name} "
                 f"({', '.join(longer.columns)}), which serves the same lookups; drop it"]
                if redundant else
                ["both start with the same column; check which queries need each before "
                 "dropping one"]
            )
            out.append(Finding(
                "overlapping-indexes", "info", table,
                f"{table}: {a.name} ({', '.join(a.columns)}, {_gb(a.bytes)}) and {b.name} "
                f"({', '.join(b.columns)}, {_gb(b.bytes)}) share the leading column "
                f"{a.columns[0]}",
                plan(None, {"table": table, "indexes": sorted(pair)}, steps, sql=sql),
            ))
    return out


# -- the report ---------------------------------------------------------------------


def report(
    catalog: Catalog,
    store: SampleStore | None,
    limits: dict[str, Limit],
    *,
    th: Thresholds | None = None,
    backup: dict[str, Any] | None = None,
    disk_path: str | None = None,
    disk_usage: Callable[[str], Any] = shutil.disk_usage,
    now: float | None = None,
) -> dict[str, Any]:
    th = th or Thresholds()
    now = time.time() if now is None else now
    tables = sorted(catalog.tables(), key=lambda t: t.total_bytes, reverse=True)
    indexes = catalog.indexes()
    db_bytes = catalog.database_bytes()

    rates: dict[str, float | None] = {}
    sources: dict[str, str] = {}
    for t in tables:
        r = store.rate(t.qualified, now, t.total_bytes) if store else None
        src = "samples"
        if r is None and t.chunk_bytes_per_day is not None:
            r, src = t.chunk_bytes_per_day, "chunks"
        rates[t.qualified] = r
        sources[t.qualified] = src if r is not None else "none yet"
    db_rate = store.rate("(database)", now, db_bytes) if store and db_bytes is not None else None
    if db_rate is None:
        known = [r for r in rates.values() if r is not None]
        db_rate = sum(known) if known else None

    volume: dict[str, Any] | None = None
    notes: list[str] = []
    for path in (disk_path, os.environ.get("AXIOM_DB_DATA_PATH", "").strip(),
                 catalog.data_directory()):
        if not path:
            continue
        try:
            u = disk_usage(path)
        except OSError:
            continue
        days = (u.free / db_rate) if db_rate and db_rate > 0 else None
        volume = {"path": path, "total_bytes": u.total, "free_bytes": u.free,
                  "bytes_per_day": db_rate, "days_to_full": days}
        break
    if volume is None:
        notes.append("the database's data volume is not visible from here: pass disk_path or "
                     "set AXIOM_DB_DATA_PATH to a path on it")

    findings = judge(tables, indexes, limits, rates, th, backup)
    if volume and volume["days_to_full"] is not None and volume["days_to_full"] <= th.runway_days:
        findings.insert(0, Finding(
            "volume-runway", "critical" if volume["days_to_full"] <= 14 else "warning",
            volume["path"],
            f"the database volume has {_gb(volume['free_bytes'])} free and grows about "
            f"{_gb(db_rate)}/day: about {volume['days_to_full']:.0f} days until full",
            plan(None, {"path": volume["path"]}, [
                "act on the unbounded-growth findings first; they are where the growth is",
                "or add space to the volume",
            ]),
        ))

    if store is not None:
        sizes = {t.qualified: t.total_bytes for t in tables}
        if db_bytes is not None:
            sizes["(database)"] = db_bytes
        store.record(now, sizes)

    return {
        "measured_at": now,
        "stats_since": catalog.stats_reset(),
        "database_bytes": db_bytes,
        "volume": volume,
        "tables": [
            {
                "table": t.qualified,
                "total_bytes": t.total_bytes,
                "table_bytes": t.table_bytes,
                "index_bytes": t.index_bytes,
                "live_rows": t.live_rows,
                "dead_rows": t.dead_rows,
                "inserts": t.inserts,
                "updates": t.updates,
                "deletes": t.deletes,
                "bytes_per_day": rates[t.qualified],
                "growth_from": sources[t.qualified],
                "hypertable": t.hypertable,
                "compression": t.compression,
                "compress_after": t.compress_after,
                "retention": t.retention,
                "declared": asdict(limits[t.qualified]) if t.qualified in limits else None,
                "backup": backup_state(t.qualified, backup),
            }
            for t in tables
        ],
        "findings": [asdict(f) for f in findings],
        "thresholds": asdict(th),
        "notes": notes,
    }


def alerts(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One alert per cause, not per table: the findings grouped by rule."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in findings:
        if f["severity"] in ("warning", "critical"):
            groups.setdefault(f["code"], []).append(f)
    out = []
    for code, fs in groups.items():
        tables = [f["table"] for f in fs]
        head = fs[0]["message"] if len(fs) == 1 else (
            f"{len(fs)} tables: {', '.join(tables[:5])}{' and more' if len(tables) > 5 else ''}"
        )
        out.append({
            "code": code,
            "priority": "high" if any(f["severity"] == "critical" for f in fs) else "normal",
            "summary": f"data growth, {code.replace('-', ' ')}: {head}",
            "dedup_key": f"node.data_growth:{code}",
            "tables": tables,
        })
    return out


__all__ = [
    "Catalog", "Finding", "IndexStat", "Limit", "PostgresCatalog", "SampleStore", "TableStat",
    "Thresholds", "alerts", "backup_state", "judge", "limits_from_entries", "limits_from_rules",
    "plan", "report",
]
