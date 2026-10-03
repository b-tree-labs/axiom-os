# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Idempotency is a property of the KEY, not of the writer.

``INSERT ... ON CONFLICT DO NOTHING`` is only as strong as the thing it
conflicts *on*. Against a unique index over a sequence-backed surrogate id it
conflicts with nothing and the clause is decoration: every re-ingest appends a
complete duplicate copy.

Measured on a live node, which is why this module exists rather than a note:

    silver.signals              UNIQUE (row_hash, channel)                 safe
    reactor_timeseries_default  UNIQUE (site, reactor_id, source_class,
                                        metric, ts)                        safe
    bronze.ingested_files       UNIQUE (item_id)                           safe
    public.chunks               UNIQUE (id)  id = nextval('chunks_id_seq') UNSAFE

Three of four landing tables are re-ingest safe because their key is derived
from the data or its source. The fourth is 5.3M rows and 73 GB, and is declared
"re-derivable from bronze documents" in the backup policy — so the documented
recovery procedure for that table is the exact operation that would double it.

That is the failure this guard exists to make impossible to ship: not a writer
that forgets to dedup, but a *table* that cannot dedup no matter how careful
the writer is.

**What counts as a safe key.** Every column of the unique index must be
derivable from the source content: a content hash, a source item id, or the
natural key of the measurement (site / metric / timestamp). A column whose
default calls ``nextval`` is a counter, and a counter is different on every
run by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: A column default containing this is a sequence: unique across runs by
#: design, therefore useless as a dedup key.
_SEQUENCE_MARKER = "nextval"


@dataclass(frozen=True)
class TableVerdict:
    """Whether one landing table can survive a re-ingest."""

    schema: str
    table: str
    unique_indexes: tuple[str, ...] = ()
    key_columns: tuple[str, ...] = ()
    surrogate_columns: tuple[str, ...] = ()
    reason: str = ""

    #: The table does not exist here. Nothing lands in it, so it is neither
    #: safe nor unsafe — reporting a missing table as "cannot dedup" would fire
    #: on every fresh install and teach operators to ignore this check.
    absent: bool = False

    @property
    def safe(self) -> bool:
        """True when a re-ingest of identical data would insert nothing."""
        if self.absent:
            return True
        return bool(self.key_columns) and not self.surrogate_columns

    def __str__(self) -> str:
        where = f"{self.schema}.{self.table}"
        if self.absent:
            return f"{where}: absent — not present in this database"
        if self.safe:
            return f"{where}: SAFE — unique key ({', '.join(self.key_columns)})"
        return f"{where}: UNSAFE — {self.reason}"


def verdict_from_rows(
    schema: str,
    table: str,
    unique_index_columns: dict[str, list[str]],
    column_defaults: dict[str, str | None],
) -> TableVerdict:
    """Pure decision, so it is testable without a database.

    *unique_index_columns* maps index name to its column list; *column_defaults*
    maps column name to its SQL default (or None).
    """
    if not unique_index_columns:
        return TableVerdict(
            schema,
            table,
            reason=(
                "no unique index at all, so ON CONFLICT has nothing to conflict "
                "on and every re-ingest appends a duplicate copy"
            ),
        )

    # The most defensible key wins: prefer an index with no surrogate column.
    ranked = sorted(
        unique_index_columns.items(),
        key=lambda kv: sum(
            1 for c in kv[1] if _SEQUENCE_MARKER in (column_defaults.get(c) or "").lower()
        ),
    )
    name, cols = ranked[0]
    surrogate = tuple(c for c in cols if _SEQUENCE_MARKER in (column_defaults.get(c) or "").lower())
    if surrogate:
        return TableVerdict(
            schema,
            table,
            unique_indexes=tuple(unique_index_columns),
            key_columns=tuple(cols),
            surrogate_columns=surrogate,
            reason=(
                f"the only unique key ({name}) is over {list(surrogate)}, which "
                "is sequence-backed. A sequence is different on every run by "
                "construction, so ON CONFLICT never fires and a re-ingest "
                "duplicates the table. Key on content instead: a content hash, "
                "the source item id, or the natural key of the measurement."
            ),
        )
    return TableVerdict(
        schema,
        table,
        unique_indexes=tuple(unique_index_columns),
        key_columns=tuple(cols),
    )


_UNIQUE_SQL = """
SELECT i.relname AS index_name, a.attname AS column_name
FROM pg_class t
JOIN pg_namespace n ON n.oid = t.relnamespace
JOIN pg_index ix    ON ix.indrelid = t.oid
JOIN pg_class i     ON i.oid = ix.indexrelid
JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
WHERE ix.indisunique AND n.nspname = %(schema)s AND t.relname = %(table)s
"""

_DEFAULT_SQL = """
SELECT column_name, column_default
FROM information_schema.columns
WHERE table_schema = %(schema)s AND table_name = %(table)s
"""


_EXISTS_SQL = """
SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %(schema)s AND c.relname = %(table)s AND c.relkind = 'r'
"""


def inspect_table(cur: Any, schema: str, table: str) -> TableVerdict:
    """Read one landing table's keys through an open cursor.

    A table that is not present is reported ``absent``, never unsafe: a fresh
    install has none of them yet, and a check that fires on every new node is a
    check people learn to skip.
    """
    cur.execute(_EXISTS_SQL, {"schema": schema, "table": table})
    if cur.fetchone() is None:
        return TableVerdict(schema, table, absent=True, reason="not present")

    idx: dict[str, list[str]] = {}
    cur.execute(_UNIQUE_SQL, {"schema": schema, "table": table})
    for index_name, column_name in cur.fetchall():
        idx.setdefault(index_name, []).append(column_name)
    cur.execute(_DEFAULT_SQL, {"schema": schema, "table": table})
    defaults = {c: d for c, d in cur.fetchall()}
    return verdict_from_rows(schema, table, idx, defaults)


#: The tables THIS distribution lands rows into. A downstream package declares
#: its own through the ``axiom.data_platform.landing_tables`` entry point
#: rather than being listed here: `public.reactor_timeseries_default` belongs
#: to a nuclear package, and naming it in domain-agnostic code would both leak
#: a consumer into the public surface and make this guard useless to any other
#: tenant, who lands somewhere else entirely.
PLATFORM_LANDING_TABLES: tuple[tuple[str, str], ...] = (
    ("silver", "signals"),
    ("bronze", "ingested_files"),
    ("public", "chunks"),
)


def landing_tables() -> tuple[tuple[str, str], ...]:
    """This distribution's own landing tables (the entry-point callable)."""
    return PLATFORM_LANDING_TABLES


def audit(
    cur: Any,
    tables: tuple[tuple[str, str], ...] | None = None,
    *,
    allow: frozenset[str] | None = None,
) -> list[TableVerdict]:
    """Verdicts for every landing table, platform-declared plus discovered.

    Passing *tables* explicitly overrides discovery, which is what a test wants.
    Otherwise every portfolio package's declared tables are audited alongside
    this one's, so a nuclear package's ingest target is checked by the same
    mechanism without this module knowing it exists.
    """
    if tables is None:
        from .discovery import discover_landing_tables

        discovered, _dists = discover_landing_tables(allow=allow)
        seen: list[tuple[str, str]] = list(PLATFORM_LANDING_TABLES)
        for pair in discovered:
            if pair not in seen:
                seen.append(pair)
        tables = tuple(seen)
    return [inspect_table(cur, s, t) for s, t in tables]


__all__ = [
    "PLATFORM_LANDING_TABLES",
    "TableVerdict",
    "audit",
    "inspect_table",
    "landing_tables",
    "verdict_from_rows",
]
