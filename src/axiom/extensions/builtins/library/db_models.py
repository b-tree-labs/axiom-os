# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The library's catalog: one row per filed document, and one per thing done to it.

The bytes are not here. They are in bronze, content-addressed, with a sidecar
that records where they came from; the retrieval corpus holds their chunks. This
row is what joins the two to a person: which site it belongs to, who put it
there, who may see it, what it was decided to be and whether retrieval can find
it yet.

``site`` and ``owner`` come from the verified caller, never from a request body,
so a row cannot be filed under somebody else's name. A deleted document keeps
its row with ``deleted_at`` set: the audit trail and the bronze sidecar both
point at it, and a tombstone is how "it was here, and who removed it" stays
answerable.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, BigInteger, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class LibraryItem(Base):
    __tablename__ = "library_items"
    __table_args__ = (
        Index("ix_library_items_site_live", "site", "deleted_at"),
        Index("ix_library_items_site_sha", "site", "sha256"),
        Index("ix_library_items_site_folder", "site", "folder"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site: Mapped[str] = mapped_column(String(64))
    owner: Mapped[str] = mapped_column(String(200))
    #: ``site`` (everyone granted at the site) or ``private`` (the owner).
    visibility: Mapped[str] = mapped_column(String(16), default="site")

    file_name: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(128))
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    #: ``clean`` or ``not-scanned`` (see ``limits.scan``).
    scan: Mapped[str] = mapped_column(String(16), default="not-scanned")

    #: Placement, as intake decided it (``intake.IntakeResult``).
    taxonomy: Mapped[str] = mapped_column(String(64), default="")
    document_type: Mapped[str] = mapped_column(String(64), default="other")
    category: Mapped[str] = mapped_column(String(64), default="documents")
    label: Mapped[str] = mapped_column(String(128), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    classifier: Mapped[str] = mapped_column(String(64), default="fallback")
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)

    tags: Mapped[list] = mapped_column(JSON, default=list)
    collection: Mapped[str] = mapped_column(String(128), default="")

    #: Where the bytes and the record are (relative to the library's bronze root).
    bronze_record: Mapped[str] = mapped_column(Text, default="")
    bronze_content: Mapped[str] = mapped_column(Text, default="")
    #: The retrieval key: the corpus and the source path the chunks are filed under.
    corpus: Mapped[str] = mapped_column(String(200), default="")
    source_path: Mapped[str] = mapped_column(Text, default="")
    #: ``indexed`` | ``pending`` | ``unsupported`` | ``failed``.
    index_status: Mapped[str] = mapped_column(String(16), default="pending")
    index_reason: Mapped[str] = mapped_column(Text, default="")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str] = mapped_column(String(200), default="")

    #: ADR-213. Where the Data Book files it, and why (``book.place.Placement``).
    folder: Mapped[str] = mapped_column(String(255), default="", nullable=True)
    placement: Mapped[dict] = mapped_column(JSON, default=dict, nullable=True)
    #: The latest pipeline run's state (``done``, ``partial``, ``held``...).
    pipeline_state: Mapped[str] = mapped_column(String(16), default="", nullable=True)
    job_id: Mapped[str] = mapped_column(String(36), default="", nullable=True)
    #: The export-control hook's verdict (``extract.ec.Verdict``).
    ec: Mapped[dict] = mapped_column(JSON, default=dict, nullable=True)


class LibraryJob(Base):
    """One pipeline run over one file: stages, progress, and what went wrong (ADR-213 D2)."""

    __tablename__ = "library_jobs"
    __table_args__ = (Index("ix_library_jobs_owner", "site", "owner", "updated_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site: Mapped[str] = mapped_column(String(64))
    owner: Mapped[str] = mapped_column(String(200))
    #: ``upload`` | ``reextract`` | ``retry``.
    kind: Mapped[str] = mapped_column(String(16), default="upload")
    item_id: Mapped[str] = mapped_column(String(36), default="", nullable=True)
    upload_id: Mapped[str] = mapped_column(String(36), default="", nullable=True)
    file_name: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_received: Mapped[int] = mapped_column(BigInteger, default=0)
    #: ``queued`` | ``running`` | ``done`` | ``partial`` | ``failed`` | ``held`` | ``cancelled``.
    state: Mapped[str] = mapped_column(String(16), default="queued")
    stage: Mapped[str] = mapped_column(String(16), default="received")
    stages: Mapped[list] = mapped_column(JSON, default=list)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[dict] = mapped_column(JSON, default=dict, nullable=True)
    #: What the run was asked to do (re-extract only these, the upload's options...).
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class LibraryUpload(Base):
    """A resumable upload session: bytes arrive at offsets into a staged file."""

    __tablename__ = "library_uploads"
    __table_args__ = (Index("ix_library_uploads_owner", "site", "owner", "state"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site: Mapped[str] = mapped_column(String(64))
    owner: Mapped[str] = mapped_column(String(200))
    job_id: Mapped[str] = mapped_column(String(36))
    file_name: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(BigInteger)
    offset: Mapped[int] = mapped_column(BigInteger, default=0)
    #: The whole file's SHA-256 as the client declared it, verified before storing.
    declared_sha256: Mapped[str] = mapped_column(String(64), default="")
    #: The staged bytes, relative to the library's bronze root.
    staged_path: Mapped[str] = mapped_column(Text)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    #: ``open`` | ``complete`` | ``cancelled`` | ``expired``.
    state: Mapped[str] = mapped_column(String(16), default="open")
    chunk_size: Mapped[int] = mapped_column(Integer, default=8 * 1024 * 1024)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LibraryExtraction(Base):
    """What one extractor version found in one file: the provenance of every derived output (ADR-213 D8)."""

    __tablename__ = "library_extractions"
    __table_args__ = (Index("ix_library_extractions_item", "item_id", "current"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(String(36))
    job_id: Mapped[str] = mapped_column(String(36))
    extractor: Mapped[str] = mapped_column(String(128))
    version: Mapped[str] = mapped_column(String(32))
    scope: Mapped[str] = mapped_column(String(16), default="core")
    contributed_by: Mapped[str] = mapped_column(String(128), default="")
    #: The archive member (``dir/file.csv``) the result is about; empty for the file itself.
    member: Mapped[str] = mapped_column(Text, default="")
    #: ``done`` | ``failed`` | ``timeout`` | ``not-yet`` | ``unavailable`` | ``skipped``.
    status: Mapped[str] = mapped_column(String(16))
    summary: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    #: Bronze-derived record of this result, relative to the bronze root.
    derived_path: Mapped[str] = mapped_column(Text, default="")
    input_sha256: Mapped[str] = mapped_column(String(64), default="")
    output_sha256: Mapped[str] = mapped_column(String(64), default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    remedy: Mapped[str] = mapped_column(Text, default="")
    #: The latest result for (item, extractor, member); older rows are history.
    current: Mapped[bool] = mapped_column(default=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class LibraryRoute(Base):
    """Where one destination tag of one extraction went (ADR-213 D7)."""

    __tablename__ = "library_routes"
    __table_args__ = (Index("ix_library_routes_item", "item_id", "current"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(String(36))
    job_id: Mapped[str] = mapped_column(String(36))
    extractor: Mapped[str] = mapped_column(String(128), default="")
    tag: Mapped[str] = mapped_column(String(32))
    route: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(16))
    target: Mapped[str] = mapped_column(String(255), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[dict] = mapped_column(JSON, default=dict, nullable=True)
    remedy: Mapped[str] = mapped_column(Text, default="")
    current: Mapped[bool] = mapped_column(default=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class LibraryFolder(Base):
    """A Data Book folder a person made, or their rename/move of a scheme folder (ADR-213 D11)."""

    __tablename__ = "library_folders"
    __table_args__ = (Index("ix_library_folders_site", "site", "deleted_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site: Mapped[str] = mapped_column(String(64))
    #: The folder's path in the Book (``reports/quarterly``).
    path: Mapped[str] = mapped_column(String(255))
    label: Mapped[str] = mapped_column(String(128))
    #: ``user`` (made by a person) or ``override`` (a scheme folder renamed or moved).
    kind: Mapped[str] = mapped_column(String(16), default="user")
    #: For an override, where the scheme folder now shows (a move); empty when only renamed.
    shown_under: Mapped[str] = mapped_column(String(255), default="")
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LibraryCorrection(Base):
    """A person moved an item: feedback that the next similar item follows (ADR-213 D11)."""

    __tablename__ = "library_corrections"
    __table_args__ = (Index("ix_library_corrections_site", "site"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site: Mapped[str] = mapped_column(String(64))
    item_id: Mapped[str] = mapped_column(String(36))
    principal: Mapped[str] = mapped_column(String(200))
    from_path: Mapped[str] = mapped_column(String(255), default="")
    to_path: Mapped[str] = mapped_column(String(255))
    signature: Mapped[list] = mapped_column(JSON, default=list)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class LibraryEvent(Base):
    """Every upload, change, delete and restore, with who and from where."""

    __tablename__ = "library_events"
    __table_args__ = (Index("ix_library_events_item", "item_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(String(36))
    site: Mapped[str] = mapped_column(String(64))
    principal: Mapped[str] = mapped_column(String(200))
    #: ``upload`` | ``update`` | ``delete`` | ``restore`` | ``duplicate``.
    action: Mapped[str] = mapped_column(String(16))
    surface: Mapped[str] = mapped_column(String(32), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


__all__ = [
    "Base",
    "LibraryCorrection",
    "LibraryEvent",
    "LibraryExtraction",
    "LibraryFolder",
    "LibraryItem",
    "LibraryJob",
    "LibraryRoute",
    "LibraryUpload",
]
