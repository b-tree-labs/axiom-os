# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Apply the partition plan: create ahead, retire behind.

Both operations are **dry-run by default**. `ensure` creates tables and `retire`
drops them; a lifecycle job that does either without being asked twice is one
misconfiguration away from deleting a year of telemetry.

Every statement runs under a short `lock_timeout`. A maintenance job must never
queue in front of the writers on a live table — failing fast and saying so is
recoverable, blocking ingest behind a partition job is not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .partitions import (
    DEFAULT_LOCK_TIMEOUT,
    PartitionSpec,
    _ident,
    plan_partitions,
    report,
)

log = logging.getLogger(__name__)


@dataclass
class LifecycleOutcome:
    """What was planned, what was done, and what refused — reported separately
    so "ensured" is never mistaken for "nothing needed doing"."""

    planned: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = True

    def as_dict(self) -> dict:
        return {
            "planned": list(self.planned),
            "applied": list(self.applied),
            "skipped": list(self.skipped),
            "errors": list(self.errors),
            "dry_run": self.dry_run,
        }


def ensure(
    cur,
    spec: PartitionSpec,
    *,
    apply: bool = False,
    now: datetime | None = None,
    lock_timeout: str = DEFAULT_LOCK_TIMEOUT,
) -> LifecycleOutcome:
    """Create the partitions that should exist and do not.

    **Refuses when the DEFAULT partition holds rows and is unguarded.** Creating
    a partition then makes Postgres scan DEFAULT under ACCESS EXCLUSIVE to prove
    no row belongs in the new range, which stops every writer for the length of
    a full-table scan. On the table that motivated this module that is 174 GB.
    Guard DEFAULT with a validated CHECK first (`default_guard_sql`), or create
    partitions before the table has data.
    """
    now = now or datetime.now(UTC)
    rep = report(cur, spec, now=now)
    out = LifecycleOutcome(dry_run=not apply, planned=list(rep.missing))
    if not rep.partition_key:
        out.errors.append(f"{rep.parent} is not a partitioned table")
        return out
    if rep.default_occupied and not _default_is_guarded(cur, spec, rep.default_partition):
        out.skipped = list(rep.missing)
        out.planned = []
        out.errors.append(
            f"refusing to create partitions: {rep.default_partition} holds ~{rep.default_rows:,} rows "
            "and has no validated upper-bound CHECK, so each CREATE would scan it under "
            "ACCESS EXCLUSIVE and block writers. Guard it first."
        )
        return out
    existing = {name for name, _b, _r in rep.partitions}
    for planned in plan_partitions(now, spec, existing):
        if not apply:
            continue
        try:
            cur.execute(f"SET LOCAL lock_timeout = '{lock_timeout}'")
            cur.execute(planned.create_sql(spec))
            out.applied.append(planned.name)
        except Exception as exc:  # noqa: BLE001 - one partition failing must not stop the rest
            out.errors.append(f"{planned.name}: {type(exc).__name__}: {exc}")
    return out


def _default_is_guarded(cur, spec: PartitionSpec, default_partition: str | None) -> bool:
    """Is there a VALIDATED check constraint on DEFAULT bounding the key?"""
    if not default_partition:
        return False
    try:
        cur.execute(
            "SELECT count(*) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = %s AND t.relname = %s AND c.contype = 'c' AND c.convalidated",
            (spec.schema, default_partition),
        )
        row = cur.fetchone()
        return bool(row and row[0])
    except Exception as exc:  # noqa: BLE001
        log.debug("could not read constraints on %s: %s", default_partition, exc)
        return False


def retire(
    cur,
    spec: PartitionSpec,
    *,
    apply: bool = False,
    now: datetime | None = None,
    lock_timeout: str = DEFAULT_LOCK_TIMEOUT,
) -> LifecycleOutcome:
    """Drop partitions lying entirely before the retention window.

    A DROP returns the space immediately, indexes included, with no VACUUM and
    no bloat — which is the reason to partition a time-series table at all. The
    DEFAULT partition is never a candidate, whatever it holds: it has no upper
    bound, so it can always contain rows inside the window.
    """
    now = now or datetime.now(UTC)
    rep = report(cur, spec, now=now)
    out = LifecycleOutcome(dry_run=not apply, planned=list(rep.retirable))
    if spec.retain_periods is None:
        out.errors.append(f"{rep.parent} has no retention configured; nothing is dropped")
        out.planned = []
        return out
    for name in rep.retirable:
        if name == rep.default_partition:  # pragma: no cover - defensive
            out.skipped.append(name)
            continue
        if not apply:
            continue
        try:
            cur.execute(f"SET LOCAL lock_timeout = '{lock_timeout}'")
            cur.execute(f"DROP TABLE {_ident(spec.schema, 'schema')}.{_ident(name, 'partition')}")
            out.applied.append(name)
        except Exception as exc:  # noqa: BLE001
            out.errors.append(f"{name}: {type(exc).__name__}: {exc}")
    return out


__all__ = ["LifecycleOutcome", "ensure", "retire"]
