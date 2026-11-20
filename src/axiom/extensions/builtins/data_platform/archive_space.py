# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A site's local archive frees space by retention before its disk fills.

The archive keeps two things on disk: the bronze batches its ingest landed
(with the outbox that orders them) and ``silver.signals`` in its database.
Both grow forever unless something rotates them out. This is that something,
under a declared policy, and it is the only thing here that deletes data.

**When it acts.** Only under pressure: the disk holding the archive is below
its alarm floor (:func:`..ingest_sink.headroom.headroom`), or the archive's own
bytes (bronze plus silver) exceed a declared budget. Without pressure it does
nothing, however old the data.

**What it may delete.** Only data older than the declared minimum
(``AXIOM_ARCHIVE_KEEP_DAYS``). With no minimum declared it deletes nothing and
the alarm stays up: deleting a site's history is never implicit.

**What it never deletes.** On a contributor node, a batch the forwarder has not
yet delivered upstream (the forwarder's cursor is the boundary: it advances only
on a confirmed delivery), nor one a declared edge downstream has not pulled.
Those batches are the copy that has not reached anywhere else.

**Order.** Oldest first, and only as much as clears the pressure:

1. bronze batches already delivered, through the same retention the edge uses
   for its downstreams (:func:`..ingest_sink.edge_retention.prune`), so the
   outbox is never rewritten and sequence numbers never restart;
2. then the oldest ``silver.signals`` chunks (a time-partitioned archive only),
   whole chunks at a time. Silver is a derived copy: what is forwarded upstream
   is bronze, and a reading whose bronze is still kept is re-derived by the
   next conform pass.

Settings, all optional:

- ``AXIOM_ARCHIVE_KEEP_DAYS``: the declared minimum. Nothing younger is deleted.
- ``AXIOM_ARCHIVE_BUDGET_BYTES``: the archive's own budget (bronze plus silver).
- the disk alarm floors of :mod:`..ingest_sink.headroom`.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .ingest_sink.edge import EdgeOutbox
from .ingest_sink.edge_retention import EdgeAcks, downstreams, prune, pruned_through
from .ingest_sink.headroom import headroom

log = logging.getLogger("axiom.data.archive")

KEEP_DAYS_ENV = "AXIOM_ARCHIVE_KEEP_DAYS"
BUDGET_ENV = "AXIOM_ARCHIVE_BUDGET_BYTES"

#: The last pass, under the state dir; node status reads it.
STATUS_FILE = Path("archive") / "reclaim.json"


@dataclass(frozen=True)
class ArchivePolicy:
    #: Nothing younger than this many days is ever deleted. ``None``: nothing is.
    keep_days: float | None = None
    #: The archive's own bytes (bronze plus silver) may not exceed this.
    budget_bytes: int | None = None

    @classmethod
    def from_env(cls) -> ArchivePolicy:
        keep = os.environ.get(KEEP_DAYS_ENV, "").strip()
        budget = os.environ.get(BUDGET_ENV, "").strip()
        policy = cls(
            keep_days=float(keep) if keep else None,
            budget_bytes=int(float(budget)) if budget else None,
        )
        if policy.keep_days is not None and policy.keep_days < 0:
            raise ValueError(f"{KEEP_DAYS_ENV} must not be negative")
        return policy


class _Positions:
    """What :func:`prune` reads from an acks file, given directly."""

    def __init__(self, positions: dict[str, int]) -> None:
        self._p = positions

    def positions(self) -> dict[str, int]:
        return dict(self._p)


def _tree_bytes(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def bronze_bytes(roots: Iterable[str | os.PathLike]) -> int:
    """Bytes under each bronze root (each counted once)."""
    seen = {Path(r).resolve() for r in roots}
    return sum(_tree_bytes(r) for r in sorted(seen) if r.is_dir())


def _hypertables(cur) -> list[str]:
    try:
        cur.execute(
            "SELECT hypertable_name FROM timescaledb_information.hypertables "
            "WHERE hypertable_schema = 'silver' AND hypertable_name IN "
            "('signals', 'signal_uncertainty')"
        )
        return [r[0] for r in cur.fetchall()]
    except Exception:  # noqa: BLE001 - no TimescaleDB: a plain table
        cur.connection.rollback()
        return []


def silver_bytes(conn) -> int:
    """Bytes ``silver.signals`` (and its uncertainty terms) take, indexes included."""
    with conn.cursor() as cur:
        hyper = _hypertables(cur)
        total = 0
        for table in ("signals", "signal_uncertainty"):
            if table in hyper:
                cur.execute("SELECT hypertable_size(%s)", (f"silver.{table}",))
            else:
                cur.execute("SELECT COALESCE(pg_total_relation_size(to_regclass(%s)), 0)",
                            (f"silver.{table}",))
            total += int(cur.fetchone()[0] or 0)
        return total


def pressure(
    *,
    bronze_roots: Iterable[str | os.PathLike],
    conn: Any | None,
    policy: ArchivePolicy,
    disk_path: str | os.PathLike | None,
) -> dict[str, Any]:
    """Whether the archive must free space, and why."""
    roots = list(bronze_roots)
    disk = headroom(disk_path) if disk_path is not None else None
    used_bronze = bronze_bytes(roots)
    used_silver = silver_bytes(conn) if conn is not None else 0
    used = used_bronze + used_silver
    reasons = []
    if disk is not None and disk["alarm"]:
        reasons.append(
            f"{disk['free_bytes'] // 2**20} MiB free at {disk['path']} "
            f"(alarm below {disk['alarm_bytes'] // 2**20} MiB)"
        )
    if policy.budget_bytes is not None and used > policy.budget_bytes:
        reasons.append(
            f"the archive holds {used // 2**20} MiB, over its budget of "
            f"{policy.budget_bytes // 2**20} MiB"
        )
    return {
        "alarm": bool(reasons),
        "reasons": reasons,
        "used_bytes": used,
        "bronze_bytes": used_bronze,
        "silver_bytes": used_silver,
        "budget_bytes": policy.budget_bytes,
        "disk": disk,
    }


def delivered_through(
    outbox: EdgeOutbox, *, forwarded_through: int | None, edge_downstreams: list[str]
) -> int:
    """The highest outbox seq every copy elsewhere already holds.

    ``forwarded_through`` is the forwarder's cursor on a contributor node, or
    ``None`` on a node that forwards nothing. Each declared edge downstream's
    acknowledged position bounds it too.
    """
    bound = outbox.last_seq() if forwarded_through is None else int(forwarded_through)
    if edge_downstreams:
        acks = EdgeAcks(outbox.dir).positions()
        bound = min([bound, *(acks.get(p, 0) for p in edge_downstreams)])
    return max(0, bound)


def _oldest_silver_chunk(cur, cutoff: datetime) -> tuple[str, datetime] | None:
    cur.execute(
        "SELECT chunk_schema || '.' || chunk_name, range_end "
        "FROM timescaledb_information.chunks "
        "WHERE hypertable_schema = 'silver' AND hypertable_name = 'signals' "
        "AND range_end <= %s ORDER BY range_start LIMIT 1",
        (cutoff,),
    )
    row = cur.fetchone()
    return (row[0], row[1]) if row else None


def reclaim(
    *,
    outbox_dir: str | os.PathLike,
    bronze_root_for: Callable[[str], str | os.PathLike],
    bronze_roots: Iterable[str | os.PathLike],
    policy: ArchivePolicy,
    forwarded_through: int | None,
    dsn: str | None = None,
    connect: Callable[[str], Any] | None = None,
    disk_path: str | os.PathLike | None = None,
    edge_downstreams: list[str] | None = None,
    now: float | None = None,
    step: int = 50,
) -> dict[str, Any]:
    """Free space oldest first until the pressure clears. Returns what it did.

    ``forwarded_through`` is the forwarder's cursor on a contributor node, or
    ``None`` on a node that forwards nothing upstream.
    """
    now = time.time() if now is None else now
    roots = list(bronze_roots)
    outbox = EdgeOutbox(outbox_dir)
    edge_downstreams = downstreams() if edge_downstreams is None else edge_downstreams
    conn = None
    if dsn:
        if connect is None:
            import psycopg

            connect = psycopg.connect
        conn = connect(dsn)
        conn.autocommit = True
    try:
        before = pressure(bronze_roots=roots, conn=conn, policy=policy, disk_path=disk_path)
        report: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(),
            "before": before,
            "deleted_batches": 0,
            "pruned_through": pruned_through(outbox),
            "dropped_chunks": [],
            "kept_undelivered": max(0, outbox.last_seq() - delivered_through(
                outbox, forwarded_through=forwarded_through, edge_downstreams=edge_downstreams)),
            "notes": [],
        }
        state = before
        if state["alarm"] and policy.keep_days is None:
            report["notes"].append(
                f"no retention declared ({KEEP_DAYS_ENV} unset): nothing is deleted; "
                "free space or declare how long the archive keeps data"
            )
        elif state["alarm"]:
            min_age_hours = policy.keep_days * 24.0
            bound = delivered_through(
                outbox, forwarded_through=forwarded_through, edge_downstreams=edge_downstreams
            )
            # 1. bronze already delivered, oldest first, a step at a time.
            start = pruned_through(outbox)
            while state["alarm"] and start < bound:
                target = min(start + max(1, step), bound)
                result = prune(
                    outbox,
                    _Positions({"archive": target}),
                    downstreams=["archive"],
                    bronze_root_for=bronze_root_for,
                    min_age_hours=min_age_hours,
                    now=now,
                )
                report["deleted_batches"] += result["deleted_batches"]
                if result["pruned_through"] <= start:
                    break  # the next batch is younger than the declared minimum
                start = result["pruned_through"]
                state = pressure(bronze_roots=roots, conn=conn, policy=policy, disk_path=disk_path)
            report["pruned_through"] = start
            # 2. then whole silver chunks past the declared minimum, oldest first.
            if state["alarm"] and conn is not None:
                with conn.cursor() as cur:
                    hyper = _hypertables(cur)
                    if "signals" not in hyper:
                        report["notes"].append(
                            "silver is not time-partitioned here; only bronze can be rotated"
                        )
                    else:
                        cutoff = datetime.fromtimestamp(now, UTC) - timedelta(days=policy.keep_days)
                        while state["alarm"]:
                            chunk = _oldest_silver_chunk(cur, cutoff)
                            if chunk is None:
                                break
                            name, end = chunk
                            for table in hyper:
                                cur.execute("SELECT drop_chunks(%s, older_than => %s)",
                                            (f"silver.{table}", end))
                            report["dropped_chunks"].append(name)
                            state = pressure(bronze_roots=roots, conn=conn, policy=policy,
                                             disk_path=disk_path)
            if state["alarm"]:
                report["notes"].append(
                    "still under pressure after rotating everything the policy allows: "
                    f"{report['kept_undelivered']} batch(es) await delivery upstream and "
                    "nothing younger than the declared minimum is deleted. Free space or "
                    "raise the budget"
                )
        report["after"] = state
        report["alarm"] = state["alarm"]
    finally:
        if conn is not None:
            conn.close()
    if report["deleted_batches"] or report["dropped_chunks"]:
        log.warning(
            "archive retention freed space: %d bronze batch(es) through seq %d, %d silver chunk(s)",
            report["deleted_batches"], report["pruned_through"], len(report["dropped_chunks"]),
        )
    if report["alarm"]:
        log.warning("archive under pressure: %s", "; ".join(report["after"]["reasons"]))
    return report


def write_status(state_dir: str | os.PathLike, report: dict[str, Any]) -> Path:
    path = Path(state_dir) / STATUS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return path


__all__ = [
    "BUDGET_ENV",
    "KEEP_DAYS_ENV",
    "STATUS_FILE",
    "ArchivePolicy",
    "bronze_bytes",
    "delivered_through",
    "pressure",
    "reclaim",
    "silver_bytes",
    "write_status",
]
