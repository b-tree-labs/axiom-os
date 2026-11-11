# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SQLAlchemy models for the ``authz`` Postgres schema.

Per spec-governance-fabric §8.1: the authz primitive owns ``verdicts`` (every
decide() call's receipt), ``policies`` (per-resource per-intent rules),
``graduation`` (RACI graduation state per actor + intent + resource) and
``graduation_resolutions`` (which receipts a human has resolved).

Per ADR-052: all tables live in the ``authz`` schema; sessions are
obtained via ``axiom.infra.db.session_for('authz')``; Alembic
configures ``version_table_schema = 'authz'``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EXTENSION_SCHEMA = "authz"


class Base(DeclarativeBase):
    pass


class Verdict(Base):
    """One row per ``decide(envelope)`` call. The audit-trail receipt.

    Also one row per human resolution of a receipt, with the decision
    ``graduation.RESOLUTION``: those are records, not decisions.
    """

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
    Written by ``authz.graduation`` from human resolutions of a proposal.
    """

    __tablename__ = "graduation"
    __table_args__ = (
        Index(
            "uq_graduation_actor_intent_resource",
            "actor",
            "intent_class",
            "resource_pattern",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    actor: Mapped[str] = mapped_column(String, index=True, nullable=False)
    intent_class: Mapped[str] = mapped_column(
        String, index=True, nullable=False
    )
    """The full intent (``envelope.intent.value``), not its primitive."""
    resource_pattern: Mapped[str] = mapped_column(String, nullable=False)
    """A ``ResourcePattern``; the row covers every resource it matches."""

    approvals: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    threshold: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    graduated: Mapped[bool] = mapped_column(default=False, nullable=False)
    last_update: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )


class GraduationResolution(Base):
    """One row per resolved receipt, so a proposal counts at most once.

    The primary key is the refusal: it holds under a replay and a race, where a
    check before writing would not. A table of its own, not an index on
    ``verdicts``, because every decision writes to ``verdicts`` and building a
    unique index there would hold those writes back for the length of the scan.
    """

    __tablename__ = "graduation_resolutions"

    resolved_receipt_id: Mapped[str] = mapped_column(String, primary_key=True)
    resolution_id: Mapped[str] = mapped_column(String, nullable=False)
    """The ``verdicts`` row recording this resolution."""
    threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    """What it was counted against, so ``rebuild()`` can restore a lost row."""


__all__ = [
    "Base",
    "EXTENSION_SCHEMA",
    "Graduation",
    "GraduationResolution",
    "Policy",
    "Verdict",
]
