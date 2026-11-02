# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SQLAlchemy models for the ``authz`` Postgres schema.

Per spec-governance-fabric §8.1: the authz primitive owns three tables —
``verdicts`` (every decide() call's receipt), ``policies`` (per-resource
per-intent rules), ``graduation`` (RACI graduation state per actor +
intent class).

Per ADR-052: all tables live in the ``authz`` schema; sessions are
obtained via ``axiom.infra.db.session_for('authz')``; Alembic
configures ``version_table_schema = 'authz'``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EXTENSION_SCHEMA = "authz"


class Base(DeclarativeBase):
    pass


class Verdict(Base):
    """One row per ``decide(envelope)`` call. The audit-trail receipt."""

    __tablename__ = "verdicts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    """uuidv7 of the receipt."""

    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
        index=True,
    )

    # The envelope, serialized verbatim.
    actor: Mapped[str] = mapped_column(String, index=True, nullable=False)
    intent: Mapped[str] = mapped_column(String, index=True, nullable=False)
    resource: Mapped[str] = mapped_column(String, index=True, nullable=False)
    classification: Mapped[str] = mapped_column(String, nullable=False)
    capability_id: Mapped[str] = mapped_column(String, nullable=False)
    context_fragment_id: Mapped[str] = mapped_column(String, nullable=False)
    provenance_parent: Mapped[str] = mapped_column(String, nullable=False)
    federation_origin: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )
    dedup_key: Mapped[str] = mapped_column(String, index=True, nullable=False)

    # The verdict.
    decision: Mapped[str] = mapped_column(String, nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    matched_rules: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    """Names of rules that produced the verdict; for `axi audit explain`."""


class Policy(Base):
    """A rule that matches `ActionEnvelope` fields and returns a partial verdict.

    Per prd-axiom-authz §5.2. Higher ``priority`` wins on conflict; deny wins
    ties; explicit ``propose`` beats implicit permit.
    """

    __tablename__ = "policies"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    intent_pattern: Mapped[str] = mapped_column(String, nullable=False)
    actor_pattern: Mapped[str] = mapped_column(String, nullable=False)
    resource_pattern: Mapped[str] = mapped_column(String, nullable=False)
    classification: Mapped[str] = mapped_column(String, nullable=False)
    """Comma-separated list of applicable classifications, or "*"."""
    federation_origin_pattern: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    disposition: Mapped[str] = mapped_column(String, nullable=False)
    """One of permit / deny / propose / require_capability."""
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ttl: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )


class Graduation(Base):
    """RACI graduation state per (actor, intent_class, resource_pattern).

    Per prd-axiom-authz §5.3 + spec §D7. Novel actions return
    ``propose_to_human`` until ``approvals >= threshold``; one denial resets.
    """

    __tablename__ = "graduation"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    actor: Mapped[str] = mapped_column(String, index=True, nullable=False)
    intent_class: Mapped[str] = mapped_column(
        String, index=True, nullable=False
    )
    resource_pattern: Mapped[str] = mapped_column(String, nullable=False)

    approvals: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    threshold: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    graduated: Mapped[bool] = mapped_column(default=False, nullable=False)
    last_update: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )


__all__ = [
    "Base",
    "EXTENSION_SCHEMA",
    "Graduation",
    "Policy",
    "Verdict",
]
