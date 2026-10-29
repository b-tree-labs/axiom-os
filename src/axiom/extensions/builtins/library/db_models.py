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


__all__ = ["Base", "LibraryEvent", "LibraryItem"]
