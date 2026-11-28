# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Whole month partitions of the journal dropped at or past their lanes' floors (unit LD-6-A5).

The journal is an index of references, rebuildable from its sources, and each lane has a
retention class with a floor (``lanes.FLOOR_DAYS``) a site may lengthen and never shorten. A
month partition holds rows of many lanes, so it may go only when every lane in it is past its
effective retention, counted from the partition's end: one row of a lane kept for life keeps the
partition for life. Nothing here deletes a row; whole partitions only.

Dropping is a switch on the one runner (ADR-197): :func:`preview` names each partition and why it
stays or may go; :func:`commit` detaches the ones that may go (reversible: ``rollback`` attaches
them again); :func:`drop` is the retire, by a named approver, after the rule is checked again,
with the deletion written to the action chain before any partition is dropped. A cleanup job
never drops one on its own.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text

from . import lanes, store

KIND = "journal.prune"
_PARTITION = re.compile(r"^journal_events_p(\d{4})(\d{2})$")


def _month_end(year: int, month: int) -> datetime:
    return datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=UTC)


def _partitions(s: Any) -> list[str]:
    rows = s.execute(text(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        "WHERE i.inhparent = to_regclass('journal_events') ORDER BY c.relname")).scalars()  # fmt: skip
    return [r for r in rows if _PARTITION.match(r)]


def _lanes_in(s: Any, name: str) -> list[str]:
    if not _PARTITION.match(name):
        raise ValueError(f"refusing to read {name!r}: not a journal partition")
    # nosemgrep: avoid-sqlalchemy-text -- the table name matched journal_events_p<YYYYMM>; no value
    return sorted(s.execute(text(f'SELECT DISTINCT lane FROM "{name}"')).scalars())


def _judge(name: str, present: list[str], now: datetime) -> dict[str, Any]:
    m = _PARTITION.match(name)
    assert m is not None
    year, month = int(m.group(1)), int(m.group(2))
    end = _month_end(year, month)
    keep_until: datetime | None = end
    kept_for: list[str] = []
    for lane in present:
        try:
            days = lanes.retention(lane)["effective_days"]
        except Exception:  # noqa: BLE001 - a lane nobody declares now: kept, never guessed
            days = None
        if days is None:
            keep_until = None
            kept_for.append(lane)
            continue
        until = end + timedelta(days=int(days))
        if until > now:
            kept_for.append(lane)
        if keep_until is not None and until > keep_until:
            keep_until = until
    droppable = keep_until is not None and keep_until <= now and bool(present)
    return {"name": name, "month": f"{year:04d}-{month:02d}", "lanes": present,
            "keep_until": keep_until.isoformat() if keep_until else None,
            "droppable": droppable, "kept_for": [] if droppable else kept_for}  # fmt: skip


def preview(*, now: datetime | None = None) -> dict[str, Any]:
    """Every attached month partition: its lanes, how long it is kept and whether it may go now."""
    now = now or datetime.now(UTC)
    with store.session_scope() as s:
        out = [_judge(n, _lanes_in(s, n), now) for n in _partitions(s)]
    return {"now": now.isoformat(), "partitions": out}


class PruneHooks:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def prepare(self, ctx: Any) -> None:
        if "partitions" not in ctx.state:
            ctx.state["partitions"] = [p for p in preview(now=self.now)["partitions"] if p["droppable"]]
            ctx.state["now"] = self.now.isoformat()

    def _still_due(self, ctx: Any) -> tuple[bool, str]:
        now = datetime.fromisoformat(ctx.state.get("now") or self.now.isoformat())
        for p in ctx.state.get("partitions") or []:
            again = _judge(p["name"], p["lanes"], now)
            if not again["droppable"]:
                return False, f"{p['name']} is kept for {', '.join(again['kept_for'])}"
        return True, f"{len(ctx.state.get('partitions') or [])} partition(s) past every lane's floor"

    def probe(self, ctx: Any) -> list[Any]:
        from axiom.infra.switchplan import Probe

        return [Probe(name="past-the-floor", kind="callable", when="pre", fn=self._still_due),
                Probe(name="still-past-the-floor", kind="callable", when="post", fn=self._still_due)]  # fmt: skip

    def commit(self, ctx: Any, scope: Any) -> str:
        names = [p["name"] for p in ctx.state.get("partitions") or []]
        with scope.holding(), store.session_scope() as s:
            for n in names:
                if not _PARTITION.match(n):
                    raise ValueError(f"refusing to detach {n!r}")
                # nosemgrep: avoid-sqlalchemy-text -- the name matched journal_events_p<YYYYMM>
                s.execute(text(f'ALTER TABLE journal_events DETACH PARTITION "{n}"'))
            s.commit()
        return f"{len(names)} partition(s) detached; reads no longer see them"

    def rollback(self, ctx: Any) -> str:
        names = []
        with store.session_scope() as s:
            attached = set(_partitions(s))
            for p in ctx.state.get("partitions") or []:
                n = p["name"]
                m = _PARTITION.match(n)
                if not m or n in attached:
                    continue
                lo = datetime(int(m.group(1)), int(m.group(2)), 1, tzinfo=UTC)
                hi = _month_end(lo.year, lo.month)
                # nosemgrep: avoid-sqlalchemy-text -- the name matched journal_events_p<YYYYMM>; bounds are that month
                s.execute(text(f"ALTER TABLE journal_events ATTACH PARTITION \"{n}\" "
                               f"FOR VALUES FROM ('{lo.isoformat()}') TO ('{hi.isoformat()}')"))  # fmt: skip
                names.append(n)
            s.commit()
        return f"{len(names)} partition(s) attached again"

    def retire(self, ctx: Any) -> str:
        from axiom.infra.audit_trail import record_invocation
        from axiom.infra.paths import get_user_state_dir

        parts = ctx.state.get("partitions") or []
        # Written before anything is dropped: a drop nobody can attest never happens.
        record_invocation(state_dir=Path(get_user_state_dir()), skill="retention.delete.journal_partition",
                          params={"partitions": parts, "switch": ctx.record.id}, principal=str(ctx.params.get("approver")),
                          site=None, outcome="deleted-forever", errors_count=0)  # fmt: skip
        dropped = []
        with store.session_scope() as s:
            for p in parts:
                n = p["name"]
                if not _PARTITION.match(n):
                    raise ValueError(f"refusing to drop {n!r}")
                # nosemgrep: avoid-sqlalchemy-text -- the name matched journal_events_p<YYYYMM>; a detached partition
                s.execute(text(f'DROP TABLE IF EXISTS "{n}"'))
                dropped.append(n)
            s.commit()
        ctx.state["dropped"] = dropped
        from . import writer

        writer.forget_partitions()
        return f"dropped {', '.join(dropped) or 'nothing'}"


def _switch_dir() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return Path(get_user_state_dir()) / "journal" / "prune-switches"


def _plan(*, owner: str, now: datetime, record_id: str | None = None, approver: str | None = None) -> Any:
    from axiom.infra.switchplan import FileLedger, SwitchPlan

    return SwitchPlan(kind=KIND, subject=f"journal partitions as of {now.date().isoformat()}",
                      hooks=PruneHooks(now), ledger=FileLedger(_switch_dir()), change_kind="migration",
                      retire="reclaim", owner=owner, record_id=record_id, params={"approver": approver},
                      rollback_command="journal prune rollback")  # fmt: skip


def commit(*, owner: str, now: datetime | None = None) -> dict[str, Any]:
    """Detach every partition past every lane's floor; reversible until :func:`drop`."""
    now = now or datetime.now(UTC)
    p = _plan(owner=owner, now=now)
    p.run()
    return {"switch_id": p.record.id, "partitions": [x["name"] for x in p.record.state.get("partitions") or []]}


def drop(switch_id: str, *, approver: str, override_soak: str | None = None) -> dict[str, Any]:
    """The retire: a named approver drops the detached partitions, the rule checked again."""
    from axiom.infra.switchplan import FileLedger

    rec = FileLedger(_switch_dir()).load(switch_id)
    if rec is None:
        raise ValueError(f"no journal prune switch {switch_id}")
    now = datetime.fromisoformat(rec.state.get("now") or datetime.now(UTC).isoformat())
    p = _plan(owner=rec.retire_task.get("owner") if rec.retire_task else approver, now=now, record_id=switch_id,
              approver=approver)  # fmt: skip
    p.retire(approver=approver, override_soak=override_soak)
    return {"switch_id": switch_id, "dropped": list(p.record.state.get("dropped") or [])}


__all__ = ["KIND", "commit", "drop", "preview"]
