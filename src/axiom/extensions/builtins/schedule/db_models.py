# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SQLAlchemy models for the ``schedule`` Postgres schema.

Per spec-axiom-schedule §2.3 + spec-governance-fabric §8.4: PULSE owns
three tables — ``schedule_definition`` (the registration), ``schedule_fire_log``
(idempotency + dead-letter), ``schedule_lease`` (the leader lease).

Per ADR-052: all tables live in the ``schedule`` schema; sessions are
obtained via ``axiom.infra.db.session_for('schedule')``; Alembic
configures ``version_table_schema = 'schedule'``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EXTENSION_SCHEMA = "schedule"


class Base(DeclarativeBase):
    pass


class ScheduleDefinition(Base):
    """A registered schedule. Per spec-axiom-schedule §2.3."""

    __tablename__ = "schedule_definition"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    """uuidv7 of the schedule."""

    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    extension: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    """Owning extension if this came from a manifest block; NULL for ad-hoc."""

    action: Mapped[str] = mapped_column(String, nullable=False)
    """Dotted CallableRef the engine invokes at fire time."""

    cadence_kind: Mapped[str] = mapped_column(String, nullable=False)
    """one_shot | interval | cron | trigger"""

    cadence_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    """interval_seconds | cron_expr | trigger_spec — keyed by cadence_kind."""

    next_fire_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    """NULL only for trigger schedules awaiting a match."""

    not_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    not_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    randomized_delay_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    classification_ceiling: Mapped[str | None] = mapped_column(String, nullable=True)
    raci_default: Mapped[str] = mapped_column(
        String, nullable=False, default="autonomous"
    )

    retry_policy: Mapped[dict] = mapped_column(JSON, nullable=False)
    """max_attempts, backoff, dedup_window_seconds."""

    misfire_policy: Mapped[str] = mapped_column(
        String, nullable=False, default="fire_once"
    )
    """fire_once | fire_all | skip — what to do with instants missed while the
    engine was down (restart catch-up). Default fire_once."""

    reentrant: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """True if the action is safe to re-run after an interrupted (crashed) fire;
    drives startup reconciliation of orphaned ``pending`` fire-log rows."""

    anchor_time_slot_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )
    """If set, this cadence is dormant (next_fire_at NULL) until the slot's
    actual time is recorded, then fires relative to it. The anchor pattern."""
    anchor_to: Mapped[str | None] = mapped_column(String, nullable=True)
    """actual_start | actual_end — which actual time the fire anchors to."""
    anchor_offset_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    """Offset from the anchored actual time (e.g. 86400 = 24h after)."""

    compliance_window_seconds: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    """Max acceptable lateness. A fire later than this is a compliance violation
    (outcome ``out_of_window``), distinct from dead-letter — for protocol windows
    / queue-time limits / reporting deadlines."""
    compliance_action: Mapped[str] = mapped_column(
        String, nullable=False, default="flag"
    )
    """flag (execute but record the deviation) | skip (don't fire a late instant)."""

    capability_envelope: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    """Minted at registration time; presented at fire time. See spec §4."""

    state: Mapped[str] = mapped_column(
        String, nullable=False, default="active", index=True
    )
    """active | paused | cancelled"""

    paused_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

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


class ScheduleFireLog(Base):
    """One row per fire attempt. Idempotency log + dead-letter trail.

    Per spec-axiom-schedule §5: the unique constraint on
    ``(schedule_id, fire_time_bucket, params_hash)`` is the idempotency
    key. INSERT conflicts return the prior receipt; no re-execution.
    """

    __tablename__ = "schedule_fire_log"
    __table_args__ = (
        UniqueConstraint(
            "schedule_id",
            "fire_time_bucket",
            "params_hash",
            name="uq_schedule_fire_log_idempotency",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    schedule_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    fire_time_bucket: Mapped[int] = mapped_column(BigInteger, nullable=False)
    params_hash: Mapped[str] = mapped_column(String, nullable=False)
    intended_fire_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    outcome: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", index=True
    )
    """pending | success | failed | dead_letter"""

    receipt_fragment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class ScheduleLease(Base):
    """The singleton leader lease. Per spec-axiom-schedule §1.

    PRIMARY KEY on a constant ``TRUE`` so the table can never grow
    beyond one row.
    """

    __tablename__ = "schedule_lease"
    __table_args__ = (
        CheckConstraint("singleton IS TRUE", name="ck_schedule_lease_singleton"),
    )

    singleton: Mapped[bool] = mapped_column(
        Boolean, primary_key=True, default=True
    )
    node_id: Mapped[str] = mapped_column(String, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    renewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ScheduleTimeSlot(Base):
    """A reserved time window a consumer maps its domain onto.

    The seam (``register_time_slot`` / ``record_actual`` / ``time_slot_status``) owns this
    table. PULSE stores it and round-trips ``time_slot_metadata`` verbatim; it never
    interprets the consumer's domain. ``planned_*`` is intent; ``actual_*`` is
    what happened — the gap is the consumer's planned-vs-actual signal.
    """

    __tablename__ = "schedule_time_slot"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    planned_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    planned_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    actual_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    actual_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    time_slot_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    """Opaque consumer dict — stored and returned verbatim, never interpreted."""

    schedule_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    """Optional link to a cadence registered for this slot (reminder/timer)."""

    resource_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    """Optional scarce-resource id the consumer sets (a position / room / tool /
    antenna). Two slots with the same resource_key and overlapping windows
    conflict — PULSE uses this for conflict detection; it stays opaque otherwise."""
    fixed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """An immovable slot — conflict resolution reschedules around it, never it."""
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """Higher priority wins a conflict / preempts lower."""

    proposed_planned_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    proposed_planned_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """A pending reschedule awaiting operator confirm (the operator-veto flow)."""

    state: Mapped[str] = mapped_column(
        String, nullable=False, default="reserved", index=True
    )
    """reserved | active | done | cancelled"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )


class ScheduleBlackout(Base):
    """A window during which fires are suppressed (a maintenance outage, a
    holiday, a market closure). Instants inside an active blackout are skipped;
    the schedule resumes after the window. Global, or scoped to a resource_key.
    """

    __tablename__ = "schedule_blackout"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    ends_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    resource_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    """NULL = a global blackout (all schedules); else scoped to that resource."""
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


__all__ = [
    "Base",
    "EXTENSION_SCHEMA",
    "ScheduleBlackout",
    "ScheduleDefinition",
    "ScheduleFireLog",
    "ScheduleLease",
    "ScheduleTimeSlot",
]
