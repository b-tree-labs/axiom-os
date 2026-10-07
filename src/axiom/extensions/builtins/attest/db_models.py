# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tables for the attest schema (spec-attestation; ADR-052).

``attest_records``, ``attest_presentations`` and ``attest_anchors`` are append
only, enforced in the database by triggers (migration 0001), not by this code.
``attest_chain_heads`` and ``attest_drafts`` are working state and may change.

A record's full signed form lives in ``record``. The other columns on
``attest_records`` are copies for lookup; verification reads ``record`` only.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AttestRecord(Base):
    __tablename__ = "attest_records"
    __table_args__ = (UniqueConstraint("site_id", "logbook", "seq"),)

    attestation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(128), index=True)
    logbook: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[int] = mapped_column(BigInteger)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    prev_digest: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    meaning: Mapped[str] = mapped_column(String(32))
    entry_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signer: Mapped[str] = mapped_column(String(256), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    record: Mapped[dict] = mapped_column(JSONB)
    response_id: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)


class AttestChainHead(Base):
    __tablename__ = "attest_chain_heads"

    site_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    logbook: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, default=0)
    digest: Mapped[str] = mapped_column(String(64))


class AttestDraft(Base):
    __tablename__ = "attest_drafts"

    draft_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(128), index=True)
    logbook: Mapped[str] = mapped_column(String(64))
    entry_type: Mapped[str] = mapped_column(String(64))
    meaning: Mapped[str] = mapped_column(String(32))
    content: Mapped[dict] = mapped_column(JSONB)
    provenance: Mapped[dict] = mapped_column(JSONB)
    origin: Mapped[str] = mapped_column(String(128))
    for_principal: Mapped[str] = mapped_column(String(256), index=True)
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attestation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class AttestPresentation(Base):
    __tablename__ = "attest_presentations"

    presentation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    draft_id: Mapped[str] = mapped_column(String(36), index=True)
    digest: Mapped[str] = mapped_column(String(64))
    statement: Mapped[dict] = mapped_column(JSONB)
    rendered: Mapped[str] = mapped_column(Text)
    modality: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    presenter: Mapped[str | None] = mapped_column(String(128), nullable=True)
    forms: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    speakable: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AttestResponse(Base):
    """One answer a person gave to a presentation. Append only."""

    __tablename__ = "attest_responses"

    response_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    presentation_id: Mapped[str] = mapped_column(String(36), index=True)
    principal: Mapped[str] = mapped_column(String(256))
    answer: Mapped[str] = mapped_column(String(8))
    via: Mapped[str] = mapped_column(String(32))
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(nullable=True)
    console_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    correction: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AttestAnchor(Base):
    __tablename__ = "attest_anchors"

    anchor_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    root: Mapped[str] = mapped_column(String(64))
    heads: Mapped[list] = mapped_column(JSONB)
    node_sig: Mapped[dict] = mapped_column(JSONB)


class AttestGrantUse(Base):
    """A signing grant that signed. The key makes a second use fail."""

    __tablename__ = "attest_grant_uses"

    grant_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    presentation_id: Mapped[str] = mapped_column(String(36))
    principal: Mapped[str] = mapped_column(String(256))
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AttestDevice(Base):
    """An enrolled signing device. Administration state, not evidence."""

    __tablename__ = "attest_devices"

    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(128), index=True)
    device_class: Mapped[str] = mapped_column(String(16))
    location: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mobility: Mapped[str] = mapped_column(String(16))
    enrolled_by: Mapped[str] = mapped_column(String(256))
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retired_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
