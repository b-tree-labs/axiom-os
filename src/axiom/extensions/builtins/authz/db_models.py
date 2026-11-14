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


class AccessGrant(Base):
    """One person's or group's role on one object (ADR-263 D10, prd-sharing-and-access SHR-R1.2).

    The Owner is not a row: it stays where the object's home keeps it (a Library
    item's ``owner``, a folder's ``created_by``). A row ends by ``revoked_at`` (removed,
    replaced, expired); nothing is deleted, so who had access when stays answerable.
    """

    __tablename__ = "access_grants"
    __table_args__ = (
        Index("ix_access_grants_object", "object_kind", "object_id"),
        Index("ix_access_grants_subject", "site", "subject"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ``document`` | ``folder`` (``agent`` in a later stage).
    object_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    object_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: A person's handle (``@name:site``) or ``group:<directory group id>``.
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    #: ``viewer`` | ``commenter`` | ``editor`` (``invoker`` for an agent, later).
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    granted_by: Mapped[str] = mapped_column(String(200), nullable=False)
    #: How it came to be: ``share``, ``request`` (an approved request), ``transfer``
    #: (the old Owner kept as Editor), ``migration``.
    granted_via: Mapped[str] = mapped_column(String(16), default="share", nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: ``removed`` | ``replaced`` | ``expired``.
    revoked_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: The ``decide()`` receipt of the act that made this row.
    receipt_id: Mapped[str | None] = mapped_column(String, nullable=True)


class AccessSettings(Base):
    """An object's general access line and its two sharing settings (SHR-R2, ADR-263 D3).

    No row is the object's state before sharing existed: its home's projection
    (a Library item's ``visibility``) answers, which is how migration keeps every
    access a person has today without rewriting a row.
    """

    __tablename__ = "access_settings"

    object_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    object_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    site: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ``restricted`` or ``site``.
    general: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The role Everyone at the site has when ``general`` is ``site``.
    general_role: Mapped[str] = mapped_column(String(16), default="viewer", nullable=False)
    editors_can_share: Mapped[bool] = mapped_column(default=True, nullable=False)
    viewers_can_download: Mapped[bool] = mapped_column(default=True, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(200), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class AccessRequest(Base):
    """A person asking for access to one object, and the answer (SHR-R6)."""

    __tablename__ = "access_requests"
    __table_args__ = (
        Index("ix_access_requests_object", "object_kind", "object_id", "state"),
        Index("ix_access_requests_requester", "site", "requester"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site: Mapped[str] = mapped_column(String(64), nullable=False)
    object_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    object_id: Mapped[str] = mapped_column(String(64), nullable=False)
    requester: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: ``open`` | ``approved`` | ``declined`` | ``withdrawn``.
    state: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The role the answer gave, which may be lower than asked.
    granted_role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: The decline reason, as written.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = [
    "AccessGrant",
    "AccessRequest",
    "AccessSettings",
    "Base",
    "EXTENSION_SCHEMA",
    "Graduation",
    "GraduationResolution",
    "Policy",
    "Verdict",
]
