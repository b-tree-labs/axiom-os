# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SQLAlchemy models for the ``vault`` Postgres schema.

Per spec-governance-fabric §8.2: the vault primitive owns:

- ``capabilities`` — live tokens (id, subject, scope, expiry, issuer signature)
- ``revocations`` — revocation records, queryable for status checks
- ``outbound_receipts`` — every outbound_call invocation's audit fragment
- ``secret_refs`` — named pointers to underlying credentials

**No plaintext secret values are stored in any of these tables.** Secrets
themselves live in the OS-native keychain / Secret Service / Credential
Manager (Phase 2) or in 0600-protected credential files (Phase 1 default,
via the existing ``axiom.infra.connections.get_credential`` chain).

The ``secret_refs`` table is metadata only: name, location-hint, classification.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EXTENSION_SCHEMA = "vault"


class Base(DeclarativeBase):
    pass


class Capability(Base):
    """A live capability token. The cryptographic primitive is the token's
    signature (held in-memory by KEEP); this row is the authoritative
    record of issuance + scope + lifecycle.
    """

    __tablename__ = "capabilities"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    issuer: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str] = mapped_column(String, index=True, nullable=False)

    intent_pattern: Mapped[str] = mapped_column(String, nullable=False)
    resource_pattern: Mapped[str] = mapped_column(String, nullable=False)
    classification_ceiling: Mapped[str] = mapped_column(String, nullable=False)

    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    not_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    delegation_depth: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_capability: Mapped[str | None] = mapped_column(String, nullable=True)

    secret_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    """Name of the underlying credential this capability dereferences to,
    if any. Outbound HTTP calls use this to look up the cleartext via
    the ``axiom.infra.connections.get_credential`` chain."""

    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )


class Revocation(Base):
    __tablename__ = "revocations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    capability_id: Mapped[str] = mapped_column(
        String, nullable=False, index=True
    )
    revoked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class SecretRef(Base):
    """Metadata for a named credential. **No values stored here.**"""

    __tablename__ = "secret_refs"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    classification: Mapped[str] = mapped_column(String, nullable=False)
    backend: Mapped[str] = mapped_column(String, nullable=False)
    """Backend identifier: ``connections`` (env/settings/file chain),
    ``os_keychain`` (Phase 2), ``hashicorp_vault`` (Phase 5), etc."""
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )


class OutboundReceipt(Base):
    """One row per outbound_call invocation. Per spec §4."""

    __tablename__ = "outbound_receipts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    called_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
        index=True,
    )
    capability_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String, nullable=False, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(String, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    """succeeded / failed / capability_invalid."""
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = [
    "Base",
    "Capability",
    "EXTENSION_SCHEMA",
    "OutboundReceipt",
    "Revocation",
    "SecretRef",
]
