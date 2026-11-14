# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tables for the journal schema (design note J1 section 1; ADR-052).

``journal_events`` is partitioned by month on ``at`` and append only, enforced by triggers in
migration 0001, not by this code. ``journal_keys`` holds the ``(source, source_id)`` key once
across every partition, so a replay, a live event and a catch-up write one row. ``journal_cursors``
is working state: where each catch-up reader resumes.

There is deliberately no payload column. A row references a record; it never holds one.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Identity, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class JournalEvent(Base):
    __tablename__ = "journal_events"
    __table_args__ = {"postgresql_partition_by": "RANGE (at)"}

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lane: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(256), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(256), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    subjects: Mapped[list] = mapped_column(JSONB, nullable=False)
    ref: Mapped[dict] = mapped_column(JSONB, nullable=False)
    link: Mapped[str | None] = mapped_column(Text, nullable=True)
    site: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    logbook: Mapped[str | None] = mapped_column(String(64), nullable=True)
    classification: Mapped[str] = mapped_column(String(16), nullable=False)
    retention_class: Mapped[str] = mapped_column(String(16), nullable=False)


class JournalKey(Base):
    __tablename__ = "journal_keys"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JournalCursor(Base):
    __tablename__ = "journal_cursors"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    position: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["Base", "JournalCursor", "JournalEvent", "JournalKey"]
