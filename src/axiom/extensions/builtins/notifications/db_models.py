# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SQLAlchemy models for the ``notifications`` schema.

Per spec-axiom-notifications §8 + spec-governance-fabric §8.3 + ADR-052:
the HERALD primitive owns ``notifications.*``. The Postgres tables ship
unused at SEC-1 (the in-memory store covers all SEC-1 tests); HERALD-2
swaps the store via ``axiom.infra.db.session_for("notifications")``.

The schema is locked here so HERALD-2 ships with a deterministic
migration history rather than a fresh DDL pass.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EXTENSION_SCHEMA = "notifications"


class Base(DeclarativeBase):
    pass


class DeliveryReceipt(Base):
    """One row per ``send()`` invocation. Spec §8."""

    __tablename__ = "delivery_receipts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    envelope_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    intent: Mapped[str] = mapped_column(String, nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String, nullable=False, index=True)
    recipient: Mapped[str] = mapped_column(String, nullable=False, index=True)
    classification: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[str] = mapped_column(String, nullable=False)
    channel_selected: Mapped[str | None] = mapped_column(String, nullable=True)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    """pending|succeeded|failed|denied|expired."""
    vendor_correlation: Mapped[str | None] = mapped_column(String, nullable=True)
    correlation_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    routing_rationale: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    fragment_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )


class NotificationsInbox(Base):
    """Per-recipient row. Spec §1 + §8."""

    __tablename__ = "notifications_inbox"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    receipt_id: Mapped[str] = mapped_column(
        String, ForeignKey("notifications.delivery_receipts.id"),
        nullable=False, index=True,
    )
    recipient: Mapped[str] = mapped_column(String, nullable=False, index=True)
    classification: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[str] = mapped_column(String, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    body_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    snoozed_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    muted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )


class Thread(Base):
    """correlation_id ↔ vendor thread mapping. Spec §3."""

    __tablename__ = "threads"

    correlation_id: Mapped[str] = mapped_column(String, primary_key=True)
    receipt_id: Mapped[str] = mapped_column(
        String, ForeignKey("notifications.delivery_receipts.id"),
        nullable=False, index=True,
    )
    channel: Mapped[str] = mapped_column(String, nullable=False)
    vendor_thread_id: Mapped[str | None] = mapped_column(String, nullable=True)
    cross_channel: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )


class ChannelPreference(Base):
    """Per-recipient per-class channel ordering. Spec §8."""

    __tablename__ = "channel_preferences"

    recipient: Mapped[str] = mapped_column(String, primary_key=True)
    classification: Mapped[str] = mapped_column(String, primary_key=True)
    priority: Mapped[str] = mapped_column(String, primary_key=True)
    ordered_channels: Mapped[list] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )


class ChannelRegistryRow(Base):
    """Recipient → channel-address (via vault ref). Spec §8."""

    __tablename__ = "channel_registry"

    recipient: Mapped[str] = mapped_column(String, primary_key=True)
    channel: Mapped[str] = mapped_column(String, primary_key=True)
    address_ref: Mapped[str] = mapped_column(String, nullable=False)
    """Name of the vault.secret_refs entry; NEVER plaintext."""
    classification: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )


class DedupLog(Base):
    """Sliding-window idempotency per fabric §6.1."""

    __tablename__ = "dedup_log"

    primitive: Mapped[str] = mapped_column(
        String, primary_key=True, default="notifications"
    )
    actor: Mapped[str] = mapped_column(String, primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String, primary_key=True)
    receipt_id: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


__all__ = [
    "Base",
    "ChannelPreference",
    "ChannelRegistryRow",
    "DedupLog",
    "DeliveryReceipt",
    "EXTENSION_SCHEMA",
    "NotificationsInbox",
    "Thread",
]
