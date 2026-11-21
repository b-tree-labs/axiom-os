# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The feedback schema (ADR-052): one row per report, one row per cause (LOOP-B1 to B5).

No ``schema=`` on the tables: ``session_for("feedback")`` sets the search path, and the
migration names the schema on every op.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EXTENSION_SCHEMA = "feedback"

#: JSON on every database, JSONB on Postgres (the runtime database, ADR-174).
JSONDoc = JSON().with_variant(JSONB(), "postgresql")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Report(Base):
    """One report: good or bad, an optional sentence, and the context the server captured (B1, B2)."""

    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow,
                                                 index=True)
    reporter: Mapped[str] = mapped_column(String(255), nullable=False)
    #: ``person`` or ``agent`` when the surface knows it; else null.
    reporter_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    verdict: Mapped[str] = mapped_column(String(8), nullable=False)
    severe: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    surface: Mapped[str | None] = mapped_column(String(32), nullable=True)
    release: Mapped[str | None] = mapped_column(String(64), nullable=True)
    node: Mapped[str | None] = mapped_column(String(255), nullable=True)
    environment: Mapped[str | None] = mapped_column(String(32), nullable=True)
    capability: Mapped[str | None] = mapped_column(String(128), nullable=True)
    screen: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: For an agent's report of a call (B3): ``failed``, ``refused`` or ``unusable``.
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: ``{key: source}`` for every flag that resolved on (or to a variant) for the reporter.
    flags_on: Mapped[dict | None] = mapped_column(JSONDoc, nullable=True)
    flag: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    group_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)


class Group(Base):
    """Reports with one cause: a release, the implicated flag, and the capability or screen (B4).

    It is the ask (ADR-253): its owner, the rollback already chosen, and what was decided.
    """

    __tablename__ = "groups"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    cause_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    release: Mapped[str | None] = mapped_column(String(64), nullable=True)
    flag: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    environment: Mapped[str | None] = mapped_column(String(32), nullable=True)
    capability: Mapped[str | None] = mapped_column(String(128), nullable=True)
    screen: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    #: ``stop_flag`` (a flag is implicated) or ``roll_back_slot`` (the release is).
    rollback: Mapped[str] = mapped_column(String(16), nullable=False)
    #: ``open`` (an ask), ``stopped``, ``rolled_back``, ``fixing_forward``; null while only good reports.
    status: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    bad: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    good: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    severe: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    last_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The notification receipts sent for this ask: one when it opened, one on an automatic stop.
    notices: Mapped[list | None] = mapped_column(JSONDoc, nullable=True)


__all__ = ["Base", "EXTENSION_SCHEMA", "Group", "JSONDoc", "Report"]
