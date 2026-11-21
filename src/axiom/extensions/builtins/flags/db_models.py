# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The flags schema: flag_overrides (ADR-210), cohorts and rollouts (ADR-257 D5), per ADR-052.

No schema= on the table: session_for("flags") sets the search path, and
the migration names the schema on every op.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EXTENSION_SCHEMA = "flags"

#: The scopes an override is stored under, in resolution order (later wins).
SCOPES: tuple[str, ...] = ("node", "account", "user")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


#: JSON on every database, JSONB on Postgres (the runtime database, ADR-174).
JSONDoc = JSON().with_variant(JSONB(), "postgresql")


class FlagOverride(Base):
    """One override: this flag has this value for this node, account or user."""

    __tablename__ = "flag_overrides"

    scope: Mapped[str] = mapped_column(String(16), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(8), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    #: Why it was set (LOOP-F10). A protected flag honours only a reasoned override.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: When it stops applying (LOOP-F10); an expired override is ignored and reported.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The variant of a multivariate flag (LOOP-F17); ``value`` then holds ``on``.
    variant: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Cohort(Base):
    """A named, stored set (LOOP-F1): listed principals or accounts, or a rule, or both."""

    __tablename__ = "cohorts"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rules: Mapped[dict | None] = mapped_column(JSONDoc, nullable=True)
    principals: Mapped[list] = mapped_column(JSONDoc, nullable=False, default=list)
    accounts: Mapped[list] = mapped_column(JSONDoc, nullable=False, default=list)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The external group it follows (LOOP-F16): ``{kind, group, ...}``; ``NULL`` for a listed cohort.
    source: Mapped[dict | None] = mapped_column(JSONDoc, nullable=True)
    #: How often the scheduler refreshes it from its source; ``NULL``: only when asked.
    refresh_every_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Why the last refresh failed (the members are kept as they were); ``NULL`` when it worked.
    source_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class Rollout(Base):
    """One flag's rollout in one environment (LOOP-F4, F6): its ordered rules and its kill switch."""

    __tablename__ = "rollouts"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    environment: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: The ordered rules; ``NULL`` when none were ever set (a row may hold only a kill switch).
    rules: Mapped[list | None] = mapped_column(JSONDoc, nullable=True)
    #: ``principal`` | ``account``; ``NULL`` reads the flag's declared default. Kept for the
    #: release before LOOP-F14, which reads only this column.
    bucket_by: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: The unit a percentage buckets (LOOP-F14): any catalogue attribute or ``cohort:a,b``.
    bucket_unit: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    kill_switch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    kill_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    kill_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kill_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class StagePlan(Base):
    """One flag's progressive rollout in one environment (LOOP-F18): its stages and where it stands."""

    __tablename__ = "stage_plans"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    environment: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: ``{stages, soak, value | split, bucket_by, gates, on_fail}``.
    plan: Mapped[dict] = mapped_column(JSONDoc, nullable=False)
    #: running | halted | done | rolled_back
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    #: The current stage's index; -1 once rolled back.
    stage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stage_entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    halted_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Every transition: ``[{at, from, to, status, by, why}]``.
    history: Mapped[list] = mapped_column(JSONDoc, nullable=False, default=list)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class Change(Base):
    """A cohort, rollout or stage-plan change that waits: on approvals (LOOP-F23), on its
    time (LOOP-F19), or both; and what became of it."""

    __tablename__ = "changes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: The digest of the capability and its parameters (``skill_chain.plan_digest``).
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    params: Mapped[dict] = mapped_column(JSONDoc, nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    environment: Mapped[str | None] = mapped_column(String(32), nullable=True)
    author: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The policies that held it: ``[{approvals, roles, source}]``.
    policy: Mapped[list] = mapped_column(JSONDoc, nullable=False, default=list)
    #: ``[{by, roles, at, note}]``
    approvals: Mapped[list] = mapped_column(JSONDoc, nullable=False, default=list)
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: awaiting_approval | approved | scheduled | applied | failed | cancelled
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONDoc, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
