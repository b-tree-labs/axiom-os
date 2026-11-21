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
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
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
    # The record a supplement (and later an ack, co-signature or seal) acts on (0009).
    target: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)


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
    # The entry action this draft signs (0009): ``statement`` or ``supplement`` today; ``target``
    # and ``reason`` are set for an action on another record.
    kind: Mapped[str] = mapped_column(String(32), default="statement", server_default="statement")
    target: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # What the record will be about (0011, unit X3): ``[{kind, value, role?}]`` or None.
    subjects: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # When the person says the entry happened, if earlier than its signing (0013, #1813).
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The attachments the draft carries, by hash (0017, unit LD-4); the bytes are blobs.
    attachments: Mapped[list | None] = mapped_column(JSONB, nullable=True)


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


class AttestImportRef(Base):
    """One imported record's source reference (#1804); live while ``withdrawn_at`` is null."""

    __tablename__ = "attest_import_refs"

    attestation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(128))
    logbook: Mapped[str] = mapped_column(String(64))
    source_ref: Mapped[str] = mapped_column(Text)
    batch_id: Mapped[str] = mapped_column(String(36), index=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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


class AttestExport(Base):
    """An evidence package a person asked for (unit L6). Working state: the package is in
    storage and carries the whole chain; this row says who asked, for what, and where it is."""

    __tablename__ = "attest_exports"

    export_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(128))
    logbook: Mapped[str] = mapped_column(String(64))
    requested_by: Mapped[str] = mapped_column(String(256))
    filter: Mapped[dict] = mapped_column(JSONB)
    formats: Mapped[list] = mapped_column(JSONB)
    #: ``{chain, selected}``: how many records the package holds and how many the views select.
    counts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    storage_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AttestFieldValue(Base):
    """One field value of one entry, typed by the declaration (ADR-268 D4, migration 0015): a
    labelled projection of the chain the field filters read, rebuilt by ``field_index``."""

    __tablename__ = "attest_field_values"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    site_id: Mapped[str] = mapped_column(String(128))
    logbook: Mapped[str] = mapped_column(String(64))
    entry_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_seq: Mapped[int] = mapped_column(BigInteger)
    attestation_id: Mapped[str] = mapped_column(String(36))
    field: Mapped[str] = mapped_column(String(128))
    instrument: Mapped[str] = mapped_column(String(128), default="")
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_words: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
    value_num: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    value_unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    value_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_attestation_id: Mapped[str] = mapped_column(String(36))
    source_seq: Mapped[int] = mapped_column(BigInteger)
    source_digest: Mapped[str] = mapped_column(String(64))
    current: Mapped[bool] = mapped_column(Boolean, default=True)


class AttestFieldWatermark(Base):
    """How far the field index has projected one chain, and by which projector version."""

    __tablename__ = "attest_field_watermarks"

    site_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    logbook: Mapped[str] = mapped_column(String(64), primary_key=True)
    indexed_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    version: Mapped[str] = mapped_column(String(32))


class AttestAttachmentIndex(Base):
    """What was extracted from one attachment (ADR-251 D6, migration 0017): a text chunk or a
    metadata part, with the entry's labels, pointing at the record and the hash. Derived and
    rebuildable; never the attachment's record of truth."""

    __tablename__ = "attest_attachment_index"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    attestation_id: Mapped[str] = mapped_column(String(36))
    sha256: Mapped[str] = mapped_column(String(64))
    site_id: Mapped[str] = mapped_column(String(128))
    logbook: Mapped[str] = mapped_column(String(64))
    classification: Mapped[str] = mapped_column(String(32))
    export_controlled: Mapped[bool] = mapped_column(Boolean)
    #: ``document``, ``image``, ``video`` or ``other``: how the attachment was chunked.
    kind: Mapped[str] = mapped_column(String(16))
    #: ``text`` (a chunk of parsed, OCR or caption text) or ``metadata``.
    part: Mapped[str] = mapped_column(String(16))
    chunk: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    embedding: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    embed_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AttestArchive(Base):
    """A sealed period as a verifiable package in cold storage (unit LD-5, migration 0018).
    Working state: the package is the evidence and carries its own manifest; this row says where
    it is, what verified it and whether reads come from it.

    ``status``: ``packaged`` (written and stored), ``verified`` (a verify report for exactly this
    package digest is stored), ``archived`` (the switch committed: reads of the period come from
    the package), ``retired`` (the hot derived copies were reclaimed by a person's retire)."""

    __tablename__ = "attest_archives"
    __table_args__ = (UniqueConstraint("site_id", "logbook", "seal_id"),)

    archive_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(128))
    logbook: Mapped[str] = mapped_column(String(64))
    seal_id: Mapped[str] = mapped_column(String(36))
    from_seq: Mapped[int] = mapped_column(BigInteger)
    to_seq: Mapped[int] = mapped_column(BigInteger)
    #: the last record in the package: the seal itself
    last_seq: Mapped[int] = mapped_column(BigInteger)
    package_digest: Mapped[str] = mapped_column(String(64))
    package_sha256: Mapped[str] = mapped_column(String(64))
    storage_id: Mapped[str] = mapped_column(Text)
    storage_class: Mapped[str] = mapped_column(String(32))
    manifest: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16))
    verify_report: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    switch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_by: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AttestTombstone(Base):
    """One signed tombstone and the erasure it carries out (migration 0019, unit LD-6): working
    state. ``status`` is ``signed``, ``committed`` (reads hide the content; the database allows
    the erasure), ``erased`` or ``rolled_back``. The tombstone record itself is in the chain."""

    __tablename__ = "attest_tombstones"
    __table_args__ = (UniqueConstraint("site_id", "logbook", "seal_id", name="uq_attest_tombstones_seal"),)

    tombstone_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(128))
    logbook: Mapped[str] = mapped_column(String(64))
    seal_id: Mapped[str] = mapped_column(String(36))
    archive_id: Mapped[str] = mapped_column(String(36))
    from_seq: Mapped[int] = mapped_column(BigInteger)
    to_seq: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))
    switch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    deletion: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    erased_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_by: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AttestTierMove(Base):
    """One move of one log's year to the cold tier (migration 0020, unit LD-7): working state.
    ``status`` is ``preparing``, ``prepared`` (the cold table is built beside the hot rows),
    ``cold`` (committed: the cold table holds the rows and the chain reads them through it),
    ``retired`` (the soak passed), or ``rolled_back`` (the rows are hot again; the cold table is
    gone). ``committing`` and ``rolling_back`` exist only inside the switching transaction."""

    __tablename__ = "attest_tier_moves"

    move_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(128))
    logbook: Mapped[str] = mapped_column(String(64))
    year: Mapped[int] = mapped_column(Integer)
    from_seq: Mapped[int] = mapped_column(BigInteger)
    to_seq: Mapped[int] = mapped_column(BigInteger)
    rows: Mapped[int] = mapped_column(BigInteger)
    table_name: Mapped[str] = mapped_column(String(63))
    tablespace: Mapped[str | None] = mapped_column(String(63), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    gold_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    digests_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    archives: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    switch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
