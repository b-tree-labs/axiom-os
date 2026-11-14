# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Report and group storage behind one session seam.

Production binds ``axiom.infra.db.session_for("feedback")`` (ADR-052) against the runtime
Postgres (ADR-174); tests bind SQLite through :func:`set_provider`. Nothing else opens a
session on these tables.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func

from .db_models import Group, Report


def _default_provider() -> AbstractContextManager[Any]:
    from axiom.infra.db import session_for

    return session_for("feedback")


_provider: Callable[[], AbstractContextManager[Any]] = _default_provider


def set_provider(provider: Callable[[], AbstractContextManager[Any]] | None) -> None:
    """Bind the session provider (tests inject SQLite here); ``None`` restores the default."""
    global _provider
    _provider = provider or _default_provider


@contextlib.contextmanager
def session_scope() -> Iterator[Any]:
    with _provider() as session:
        yield session


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def _aware(at: datetime | None) -> datetime | None:
    return at.replace(tzinfo=UTC) if at is not None and at.tzinfo is None else at


def group_view(g: Group, *, reporters: int | None = None) -> dict[str, Any]:
    return {
        "id": g.id, "release": g.release, "flag": g.flag, "environment": g.environment,
        "capability": g.capability, "screen": g.screen, "owner": g.owner, "rollback": g.rollback,
        "status": g.status, "bad": g.bad, "good": g.good, "severe": g.severe,
        "reporters": reporters,
        "first_at": _aware(g.first_at).isoformat() if g.first_at else None,
        "last_at": _aware(g.last_at).isoformat() if g.last_at else None,
        "decided_by": g.decided_by, "decision_reason": g.decision_reason,
        "decided_at": _aware(g.decided_at).isoformat() if g.decided_at else None,
        "notices": list(g.notices or []),
    }


def report_view(r: Report) -> dict[str, Any]:
    return {
        "id": r.id, "created_at": _aware(r.created_at).isoformat(), "reporter": r.reporter,
        "reporter_kind": r.reporter_kind, "verdict": r.verdict, "severe": r.severe, "text": r.text,
        "surface": r.surface, "release": r.release, "node": r.node, "environment": r.environment,
        "capability": r.capability, "screen": r.screen, "outcome": r.outcome, "error": r.error,
        "flags_on": dict(r.flags_on or {}), "flag": r.flag, "group_id": r.group_id,
    }


def add(report: dict[str, Any], group: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Store one report under its cause's group. Returns ``(report, group, group_was_new)``."""
    now = datetime.now(UTC)
    with session_scope() as s:
        g = s.query(Group).filter_by(cause_key=group["cause_key"]).one_or_none()
        new = g is None
        if new:
            g = Group(id=new_id(), first_at=now, last_at=now, bad=0, good=0, severe=False, notices=[],
                      **group)
            s.add(g)
        bad = report["verdict"] == "bad"
        g.bad = (g.bad or 0) + (1 if bad else 0)
        g.good = (g.good or 0) + (0 if bad else 1)
        g.severe = bool(g.severe or (bad and report.get("severe")))
        g.last_at = now
        if bad and g.status is None:
            g.status = "open"
        r = Report(id=new_id(), created_at=now, group_id=g.id, **report)
        s.add(r)
        s.commit()
        return report_view(r), group_view(g), new


def get_group(group_id: str) -> dict[str, Any] | None:
    with session_scope() as s:
        g = s.get(Group, group_id)
        return group_view(g, reporters=_reporters(s, g.id)) if g else None


def update_group(group_id: str, **fields: Any) -> dict[str, Any] | None:
    with session_scope() as s:
        g = s.get(Group, group_id)
        if g is None:
            return None
        for k, v in fields.items():
            setattr(g, k, v)
        s.commit()
        return group_view(g, reporters=_reporters(s, g.id))


def add_notice(group_id: str, notice: dict[str, Any]) -> None:
    with session_scope() as s:
        g = s.get(Group, group_id)
        if g is not None:
            g.notices = [*(g.notices or []), notice]
            s.commit()


def _reporters(s: Any, group_id: str, *, since: datetime | None = None) -> int:
    q = s.query(func.count(func.distinct(Report.reporter))).filter(Report.group_id == group_id,
                                                                  Report.verdict == "bad")
    if since is not None:
        q = q.filter(Report.created_at >= since)
    return int(q.scalar() or 0)


def distinct_reporters(group_id: str, *, since: datetime | None = None) -> int:
    """How many different people or agents reported this cause as bad (since ``since``)."""
    with session_scope() as s:
        return _reporters(s, group_id, since=since)


def groups(*, flag: str | None = None, status: tuple[str, ...] | None = None,
           limit: int = 50) -> list[dict[str, Any]]:
    with session_scope() as s:
        q = s.query(Group)
        if flag is not None:
            q = q.filter(Group.flag == flag)
        if status is not None:
            q = q.filter(Group.status.in_(status))
        rows = q.order_by(Group.last_at.desc()).limit(limit).all()
        return [group_view(g, reporters=_reporters(s, g.id)) for g in rows]


def reports(group_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = (s.query(Report).filter(Report.group_id == group_id)
                .order_by(Report.created_at.desc()).limit(limit).all())
        return [report_view(r) for r in rows]


__all__ = ["add", "add_notice", "distinct_reporters", "get_group", "groups", "new_id", "reports",
           "session_scope", "set_provider", "update_group"]
