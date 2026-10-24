# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Session seam and repository for follow-ups (ADR-196, ADR-052).

Production binds ``axiom.infra.db.session_for('program')``; unit tests bind
SQLite through :func:`set_provider`; the real-process tests use a real
Postgres. Rows leave this module as plain dicts, detached from the session.
The journal (``followup_event``) is append-only: this module has no update or
delete for it.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from .db_models import Followup, FollowupEvent
from .followup_model import aware

_COLUMNS = (
    "id", "what", "owner", "created_by", "created_at", "original_due_at", "due_at", "state",
    "check_skill", "check_args", "next_command", "ladder", "cycle", "rung", "next_action_at",
    "schedule_id", "shared_with", "evidence", "expires_at", "acknowledged_by", "acknowledged_at",
    "closed_by", "closed_at", "closed_note", "updated_at",
)
_TIME_COLUMNS = (
    "created_at", "original_due_at", "due_at", "next_action_at", "expires_at",
    "acknowledged_at", "closed_at", "updated_at",
)


def _default_provider() -> AbstractContextManager[Any]:
    from axiom.infra.db import session_for

    return session_for("program")


_provider: Callable[[], AbstractContextManager[Any]] = _default_provider


def set_provider(provider: Callable[[], AbstractContextManager[Any]]) -> None:
    """Bind the session provider (unit tests inject SQLite here)."""
    global _provider
    _provider = provider


def reset_provider() -> None:
    global _provider
    _provider = _default_provider


@contextlib.contextmanager
def session_scope() -> Iterator[Any]:
    with _provider() as session:
        yield session


def new_id() -> str:
    return "fu-" + uuid.uuid4().hex[:8]


def _row(r: Followup) -> dict[str, Any]:
    out = {c: getattr(r, c) for c in _COLUMNS}
    for c in _TIME_COLUMNS:
        out[c] = aware(out[c])
    out["ladder"] = list(out["ladder"] or [])
    out["shared_with"] = list(out["shared_with"] or [])
    out["evidence"] = list(out["evidence"] or [])
    return out


def _event(e: FollowupEvent) -> dict[str, Any]:
    return {
        "seq": e.seq, "followup_id": e.followup_id, "at": aware(e.at), "kind": e.kind,
        "actor": e.actor, "cycle": e.cycle, "rung": e.rung, "detail": dict(e.detail or {}),
    }


def create(record: dict[str, Any], *, actor: str, now: datetime) -> dict[str, Any]:
    """Insert a follow-up and its ``created`` journal row in one transaction."""
    with session_scope() as s:
        s.add(Followup(**record))
        s.add(FollowupEvent(
            followup_id=record["id"], at=now, kind="created", actor=actor, cycle=record.get("cycle", 0),
            detail={"owner": record["owner"], "due_at": aware(record["due_at"]).isoformat(),
                    "check": record.get("check_skill")},
        ))
        s.commit()
    got = get(record["id"])
    assert got is not None
    return got


def get(followup_id: str) -> dict[str, Any] | None:
    with session_scope() as s:
        r = s.get(Followup, followup_id)
        return _row(r) if r is not None else None


def all_rows(*, states: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    with session_scope() as s:
        q = s.query(Followup)
        if states:
            q = q.filter(Followup.state.in_(states))
        return [_row(r) for r in q.order_by(Followup.due_at).all()]


def update(followup_id: str, **fields: Any) -> dict[str, Any] | None:
    with session_scope() as s:
        r = s.get(Followup, followup_id)
        if r is None:
            return None
        for k, v in fields.items():
            if k not in _COLUMNS or k in ("id", "created_at", "created_by"):
                raise KeyError(f"cannot update {k!r}")
            setattr(r, k, v)
        s.commit()
        return _row(r)


def add_event(
    followup_id: str, kind: str, actor: str, *, now: datetime, cycle: int = 0, rung: int | None = None,
    detail: dict[str, Any] | None = None, dedup_key: str | None = None,
) -> bool:
    """Append a journal row. Returns False when ``dedup_key`` was already recorded."""
    with session_scope() as s:
        s.add(FollowupEvent(
            followup_id=followup_id, at=now, kind=kind, actor=actor, cycle=cycle, rung=rung,
            detail=detail or {}, dedup_key=dedup_key,
        ))
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            return False
    return True


def events(followup_id: str, *, kind: str | None = None, cycle: int | None = None) -> list[dict[str, Any]]:
    with session_scope() as s:
        q = s.query(FollowupEvent).filter(FollowupEvent.followup_id == followup_id)
        if kind:
            q = q.filter(FollowupEvent.kind == kind)
        if cycle is not None:
            q = q.filter(FollowupEvent.cycle == cycle)
        return [_event(e) for e in q.order_by(FollowupEvent.seq).all()]


def has_event(dedup_key: str) -> bool:
    with session_scope() as s:
        return s.query(FollowupEvent).filter(FollowupEvent.dedup_key == dedup_key).count() > 0


__all__ = [
    "add_event", "all_rows", "create", "events", "get", "has_event", "new_id", "reset_provider",
    "session_scope", "set_provider", "update",
]
