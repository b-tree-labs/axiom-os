# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Program-side persistence: follow-ups and their journal (ADR-196).

The program data file (``data.json``) is the program's shared, reviewed state.
A follow-up is different in kind: it is per-principal, it changes on a clock,
and it must survive a restart with exactly-once effects. So it lives in the
extension's own Postgres schema (ADR-052; tables carry no ``schema=``, the
session provider sets the ``search_path``).

``followup`` is the current record. ``followup_event`` is its append-only
journal: nothing is ever updated or deleted there, which is how a silent stop
is visible (a follow-up past its due time with no ``delivered`` event).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Followup(Base):
    """One tracked commitment to come back to something."""

    __tablename__ = "followup"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    what: Mapped[str] = mapped_column(String(500))
    owner: Mapped[str] = mapped_column(String(200), index=True)
    created_by: Mapped[str] = mapped_column(String(200), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    original_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # open | due | acknowledged | snoozed | done | expired
    state: Mapped[str] = mapped_column(String(20), default="open", index=True)
    check_skill: Mapped[str | None] = mapped_column(String(200), nullable=True)
    check_args: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    next_command: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # [{"after_s": 0, "audience": "owner", "kind": "reminder"}, ...]
    ladder: Mapped[list] = mapped_column(JSON, default=list)
    cycle: Mapped[int] = mapped_column(Integer, default=0)
    rung: Mapped[int] = mapped_column(Integer, default=0)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    schedule_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    shared_with: Mapped[list] = mapped_column(JSON, default=list)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class FollowupEvent(Base):
    """The append-only journal. A row is never updated and never deleted.

    ``dedup_key`` is unique when set; ``attempt`` and ``delivered`` carry
    ``<kind>:<id>:<cycle>:<rung>`` so a fire re-run after a crash cannot
    record the same delivery twice.
    """

    __tablename__ = "followup_event"

    seq: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    followup_id: Mapped[str] = mapped_column(String(32), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    kind: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(200))
    cycle: Mapped[int] = mapped_column(Integer, default=0)
    rung: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    dedup_key: Mapped[str | None] = mapped_column(String(200), nullable=True, unique=True)


__all__ = ["Base", "Followup", "FollowupEvent"]
