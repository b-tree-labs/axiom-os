# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What a corpus costs on disk, and where the cost actually sits.

Corpus size was discovered as an outage. A 5.3M-chunk corpus reached 73 GB —
13.8 KB per chunk, for text that is a small fraction of that — and the first
anyone knew was a node whose cluster would no longer schedule pods. There was no
number to watch, so nobody watched it.

**Bytes per chunk is the headline**, not the total. A total says the corpus is
big, which may only mean it holds a lot. Bytes per chunk says whether each chunk
costs what it should, and that is what distinguishes an encoding problem from an
abundance of content.

Two things this deliberately does not do:

**It does not remove anything.** An index with no scans may serve a feature that
has not shipped yet. The report names it and a human decides. A tool that
deletes on its own reading of a statistic is one nobody dares run.

**It does not guess.** An unrecognised embedding type reports ``None`` rather
than a plausible width, because a wrong number standing beside correct ones is
worse than a gap. Zero chunks reports ``None`` per chunk rather than 0, which
would read as excellent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Bytes per component for the embedding types pgvector offers. Anything absent
#: is reported as unknown rather than estimated.
_VECTOR_WIDTHS: dict[str, int] = {
    "vector": 4,    # float32
    "halfvec": 2,   # float16
}

#: Access methods that store embeddings. Recognising only one would silently
#: under-report on any install that chose the other.
_VECTOR_METHODS = ("ivfflat", "hnsw")


@dataclass(frozen=True)
class IndexFact:
    """One index, as the database describes it."""

    name: str
    size_bytes: int
    definition: str
    scans: int = 0

    @property
    def is_vector(self) -> bool:
        lowered = self.definition.lower()
        return any(f"using {m}" in lowered for m in _VECTOR_METHODS)


@dataclass
class StorageReport:
    rows: int
    heap_bytes: int
    toast_bytes: int
    vector_index_bytes: int
    other_index_bytes: int
    indexes: list[IndexFact] = field(default_factory=list)
    embedding_type: str = ""
    embedding_dim: int = 0
    note: str = ""
    #: True when the row count came from the planner's estimate rather than a
    #: count. Measured live, `reltuples` overstated a 5.3M-row corpus by 11%,
    #: which moved the headline bytes-per-chunk figure from 14.8 KB to 13.0 KB.
    #: A number that looks precise and is not is worse than one labelled.
    rows_estimated: bool = False

    @property
    def total_bytes(self) -> int:
        return (self.heap_bytes + self.toast_bytes
                + self.vector_index_bytes + self.other_index_bytes)

    @property
    def bytes_per_chunk(self) -> float | None:
        """The figure that reveals an encoding problem.

        ``None`` with no rows: there is nothing to divide by, and reporting 0.0
        would read as an excellent result rather than an absent one.
        """
        if self.rows <= 0:
            return None
        return round(self.total_bytes / self.rows, 2)

    @property
    def bytes_per_vector(self) -> int | None:
        """Raw width of one stored embedding, or None if the type is unknown."""
        width = _VECTOR_WIDTHS.get(self.embedding_type)
        if width is None or self.embedding_dim <= 0:
            return None
        return width * self.embedding_dim

    def as_dict(self) -> dict[str, Any]:
        """Every figure, so the report can be checked against the database by
        hand. A measurement that cannot be cross-checked is not one."""
        return {
            "rows": self.rows,
            "heap_bytes": self.heap_bytes,
            "toast_bytes": self.toast_bytes,
            "vector_index_bytes": self.vector_index_bytes,
            "other_index_bytes": self.other_index_bytes,
            "total_bytes": self.total_bytes,
            "bytes_per_chunk": self.bytes_per_chunk,
            "rows_estimated": self.rows_estimated,
            "embedding_type": self.embedding_type,
            "embedding_dim": self.embedding_dim,
            "bytes_per_vector": self.bytes_per_vector,
            "duplicate_indexes": [list(g) for g in duplicate_expressions(self.indexes)],
            "never_scanned_indexes": [i.name for i in never_scanned(self.indexes)],
            "note": self.note,
        }


def _normalise(definition: str) -> str:
    """An index definition with its own name removed.

    The name is the one part guaranteed to differ between two otherwise
    identical indexes, so comparing definitions verbatim would find nothing,
    ever.
    """
    without_name = re.sub(r"CREATE\s+(UNIQUE\s+)?INDEX\s+\S+\s+ON\s+", "ON ", definition,
                          flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", without_name).strip().lower()


def duplicate_expressions(indexes: list[IndexFact]) -> list[tuple[str, ...]]:
    """Groups of indexes covering the identical expression.

    Two GIN indexes over the same `to_tsvector` expression were live in
    production. The planner uses either, so the second is pure cost.
    """
    groups: dict[str, list[str]] = {}
    for index in indexes:
        groups.setdefault(_normalise(index.definition), []).append(index.name)
    return [tuple(names) for names in groups.values() if len(names) > 1]


def never_scanned(indexes: list[IndexFact]) -> list[IndexFact]:
    """Indexes the planner has not used.

    Reported, never removed: one may serve a feature that has not shipped, and
    the statistic resets when statistics do.
    """
    return [i for i in indexes if i.scans == 0]


def analyze(
    *,
    rows: int,
    heap_bytes: int,
    toast_bytes: int,
    indexes: list[IndexFact],
    embedding_type: str = "",
    embedding_dim: int = 0,
    rows_estimated: bool = False,
) -> StorageReport:
    """Build the report from figures the database reported."""
    vector_bytes = sum(i.size_bytes for i in indexes if i.is_vector)
    other_bytes = sum(i.size_bytes for i in indexes if not i.is_vector)
    note = "" if rows > 0 else "no chunks: nothing to divide by"
    return StorageReport(
        rows=rows,
        heap_bytes=heap_bytes,
        toast_bytes=toast_bytes,
        vector_index_bytes=vector_bytes,
        other_index_bytes=other_bytes,
        indexes=list(indexes),
        embedding_type=embedding_type,
        embedding_dim=embedding_dim,
        note=note,
        rows_estimated=rows_estimated,
    )


__all__ = [
    "IndexFact",
    "StorageReport",
    "analyze",
    "duplicate_expressions",
    "never_scanned",
]


#: Relation-level figures. `pg_total_relation_size` minus heap minus indexes is
#: TOAST, which is where both the text and the vectors actually live
#: (`vector` is declared EXTERNAL, so it is toasted rather than inline).
SQL_RELATION = """
SELECT c.reltuples::bigint,
       pg_relation_size(c.oid),
       pg_total_relation_size(c.oid) - pg_relation_size(c.oid) - pg_indexes_size(c.oid)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relname = %s
"""

#: Per-index size, definition and scan count in one pass, so the duplicate and
#: never-scanned checks see exactly what the size figures were taken from.
SQL_INDEXES = """
SELECT i.indexrelname,
       pg_relation_size(i.indexrelid),
       pg_get_indexdef(i.indexrelid),
       i.idx_scan
FROM pg_stat_user_indexes i
WHERE i.schemaname = %s AND i.relname = %s
"""

#: The declared encoding. `format_type` renders the typmod, so a vector(768)
#: reports its dimensionality rather than just its type.
SQL_EMBEDDING = """
SELECT t.typname, format_type(a.atttypid, a.atttypmod)
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_type t ON t.oid = a.atttypid
WHERE n.nspname = %s AND c.relname = %s AND a.attname = %s AND a.attnum > 0
"""


def _dim_from_declaration(declared: str) -> int:
    """`vector(768)` -> 768. An undimensioned type reports 0, not a guess."""
    match = re.search(r"\((\d+)\)", declared or "")
    return int(match.group(1)) if match else 0


def collect(cursor, *, schema: str = "public", table: str = "chunks",
            embedding_column: str = "embedding",
            exact_count: bool = True) -> StorageReport:
    """Gather the figures from a live Postgres cursor.

    Split from :func:`analyze` so the arithmetic is testable without a database
    and the SQL is inspectable without running it.
    """
    cursor.execute(SQL_RELATION, (schema, table))
    row = cursor.fetchone()
    if not row:
        return analyze(rows=0, heap_bytes=0, toast_bytes=0, indexes=[])
    rows, heap, toast = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)

    # `reltuples` is the planner's estimate and it drifts: on a live 5.3M-row
    # corpus it read 5.9M, an 11% overstatement that moved bytes-per-chunk —
    # the headline figure — from 14.8 KB down to 13.0 KB. Count exactly, and
    # fall back to the estimate only when counting is refused, saying which was
    # used rather than presenting an estimate as a measurement.
    rows_estimated = True
    if exact_count:
        try:
            cursor.execute(
                f'SELECT count(*) FROM {schema}.{table}'  # noqa: S608 - identifiers, not input
            )
            counted = cursor.fetchone()
            if counted and counted[0] is not None:
                rows, rows_estimated = int(counted[0]), False
        except Exception:  # noqa: BLE001 — keep the estimate, labelled
            pass

    cursor.execute(SQL_INDEXES, (schema, table))
    indexes = [
        IndexFact(name=r[0], size_bytes=int(r[1] or 0), definition=r[2] or "",
                  scans=int(r[3] or 0))
        for r in (cursor.fetchall() or [])
    ]

    embedding_type, embedding_dim = "", 0
    try:
        cursor.execute(SQL_EMBEDDING, (schema, table, embedding_column))
        found = cursor.fetchone()
        if found:
            embedding_type = str(found[0] or "")
            embedding_dim = _dim_from_declaration(str(found[1] or ""))
    except Exception:  # noqa: BLE001 — a corpus need not have an embedding column
        pass

    return analyze(rows=rows, heap_bytes=heap, toast_bytes=toast, indexes=indexes,
                   embedding_type=embedding_type, embedding_dim=embedding_dim,
                   rows_estimated=rows_estimated)


def render(report: StorageReport) -> list[str]:
    """Operator-readable lines. Bytes per chunk leads, because it is the figure
    that distinguishes an expensive corpus from a large one."""
    def human(n: int) -> str:
        value = float(n)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(value) < 1024 or unit == "TB":
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} TB"

    per = report.bytes_per_chunk
    lines = [
        f"  chunks:          {report.rows:,}"
        + ("   (planner estimate, not counted)" if report.rows_estimated else ""),
        f"  total:           {human(report.total_bytes)}",
        f"  per chunk:       {human(int(per)) if per else '(' + report.note + ')'}",
        f"    heap:          {human(report.heap_bytes)}",
        f"    toast:         {human(report.toast_bytes)}   text + vectors",
        f"    vector index:  {human(report.vector_index_bytes)}",
        f"    other indexes: {human(report.other_index_bytes)}",
    ]
    if report.embedding_type:
        width = report.bytes_per_vector
        cost = f"{width} B/vector" if width else "unknown width"
        lines.append(
            f"  embedding:       {report.embedding_type}({report.embedding_dim})  {cost}"
        )
    for group in duplicate_expressions(report.indexes):
        lines.append(
            f"  DUPLICATE:       {', '.join(group)} cover the same expression; "
            f"one is pure cost"
        )
    cold = never_scanned(report.indexes)
    if cold:
        total = sum(i.size_bytes for i in cold)
        lines.append(
            f"  never scanned:   {', '.join(i.name for i in cold)} "
            f"({human(total)}) — reported, not removed"
        )
    return lines


__all__ += ["SQL_RELATION", "SQL_INDEXES", "SQL_EMBEDDING", "collect", "render"]


#: Fields a before/after comparison reports, each with before, after and delta.
_COMPARED = (
    "rows", "heap_bytes", "toast_bytes", "vector_index_bytes",
    "other_index_bytes", "total_bytes", "bytes_per_chunk",
)


def from_dict(payload: dict[str, Any]) -> StorageReport:
    """Rebuild a report from :meth:`StorageReport.as_dict`.

    A snapshot taken before a change has to be readable by the run that takes
    the after, which is a different process on a different day.
    """
    return StorageReport(
        rows=int(payload.get("rows") or 0),
        heap_bytes=int(payload.get("heap_bytes") or 0),
        toast_bytes=int(payload.get("toast_bytes") or 0),
        vector_index_bytes=int(payload.get("vector_index_bytes") or 0),
        other_index_bytes=int(payload.get("other_index_bytes") or 0),
        indexes=[],
        embedding_type=str(payload.get("embedding_type") or ""),
        embedding_dim=int(payload.get("embedding_dim") or 0),
        note=str(payload.get("note") or ""),
        rows_estimated=bool(payload.get("rows_estimated")),
    )


def compare(before: StorageReport, after: StorageReport) -> dict[str, Any]:
    """Before against after, per component, with the confounders named.

    This is how every milestone after the first proves itself, so it has to be
    harder to fool than a single total:

    **Per component, not just a total.** Dropping an index and re-embedding move
    different numbers. A total that fell says something worked; which component
    fell says what, and that is the difference between a result and a
    coincidence.

    **A changed row count confounds it.** If the corpus grew while an index was
    dropped, a flat total is not "no change", it is two changes cancelling. The
    comparison says so rather than reporting a number that reads like a result.
    ``bytes_per_chunk`` is the figure that survives a moving denominator, so it
    is reported either way.

    **An estimated row count on either side confounds it.** Measured live, the
    planner's estimate was 11% off — larger than most wins being claimed.

    **It can report a regression.** A harness that only ever reports improvement
    is an advocacy tool.
    """
    def value(report: StorageReport, field_name: str) -> Any:
        return (report.bytes_per_chunk if field_name == "bytes_per_chunk"
                else getattr(report, field_name))

    out: dict[str, Any] = {}
    for field_name in _COMPARED:
        b, a = value(before, field_name), value(after, field_name)
        delta = None if (b is None or a is None) else round(a - b, 2)
        out[field_name] = {"before": b, "after": a, "delta": delta}

    reasons: list[str] = []
    if before.rows != after.rows:
        reasons.append(
            f"row count moved {before.rows:,} -> {after.rows:,}; totals are not "
            f"directly comparable, use bytes_per_chunk"
        )
    if before.rows_estimated or after.rows_estimated:
        reasons.append(
            "a row count is a planner estimate, which measured 11% off on this "
            "corpus — larger than most reductions worth claiming"
        )

    total_delta = out["total_bytes"]["delta"] or 0
    out["confounded"] = bool(reasons)
    out["note"] = " ".join(reasons)
    # Only a clean comparison may claim an improvement.
    out["improved"] = (not reasons) and total_delta < 0
    return out


__all__ += ["compare", "from_dict"]
