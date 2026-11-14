# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The serving model for the ``webapp`` schema.

One row per (site, feed, channel). Deliberately free of domain meaning: the
platform records that a channel exists, how many rows it has and the span it
covers; what the channel *is* belongs to whichever consumer produced it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SiteCatalogChannel(Base):
    """One channel of a site's catalog, projected for fast serving.

    Replaces aggregating the gold tier on every page load. The projection is
    refreshed at ingest cadence; the API reads this table directly.
    """

    __tablename__ = "site_catalog_channel"

    site: Mapped[str] = mapped_column(String(64), primary_key=True)
    feed: Mapped[str] = mapped_column(String(128), primary_key=True)
    channel: Mapped[str] = mapped_column(String(256), primary_key=True)
    unit: Mapped[str] = mapped_column(String(64), default="")
    rows: Mapped[int] = mapped_column(Integer, default=0)
    first_ts: Mapped[str] = mapped_column(Text, default="")
    last_ts: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )


class PublicationState(Base):
    """One row per table the app database receives from gold (MVP-11-A4, A5).

    Names the gold object the rows came from and the gold version they reached,
    so nothing in the app database is unaccounted for, and so a page can say
    "as of" with the lag when the table is behind its declared freshness.
    """

    __tablename__ = "publication_state"

    app_table: Mapped[str] = mapped_column(String(128), primary_key=True)
    gold_object: Mapped[str] = mapped_column(String(256))
    #: gold's revision (``revision``) or the watermark the rows reached (``watermark``).
    gold_version: Mapped[str] = mapped_column(Text, default="")
    version_kind: Mapped[str] = mapped_column(String(16), default="watermark")
    rows: Mapped[int] = mapped_column(Integer, default=0)
    freshness_s: Mapped[int] = mapped_column(Integer, default=0)
    invalidate_on: Mapped[str] = mapped_column(String(16), default="both")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["Base", "PublicationState", "SiteCatalogChannel"]
