# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One row per (principal, surface, name)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from axiom.extensions.builtins.webapp.catalog.models import Base

#: The name a surface saves under when the person did not name anything. It is
#: "what I had last time" rather than a view somebody curated, and keeping it
#: in the same table as named views means restoring is one code path.
LAST = "last"


class SavedView(Base):
    """What a person had configured on a surface.

    ``document`` is JSON as TEXT rather than a JSON column: the platform never
    queries inside it, so a JSON type would buy nothing and would tie this to
    one database. Portability matters here — the same table has to work on the
    SQLite a developer runs and the Postgres a node runs.
    """

    __tablename__ = "saved_view"

    #: Who. Not a display name: a rename must not lose somebody's views.
    principal: Mapped[str] = mapped_column(String(256), primary_key=True)
    #: Which surface — "chart", "table". The consumer names its own.
    surface: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: Which view. `LAST` is the automatic one.
    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    document: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    #: Kept open for a later "pin this one", which is why a view is a row
    #: rather than a column on a person.
    pinned: Mapped[bool] = mapped_column(default=False, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ChannelLabel(Base):
    """A readable name laid OVER the one a channel was acquired under.

    The acquired name is never rewritten — it is the channel's identity, and a
    rename forks the data. This is a serving-time overlay and nothing else
    reads it.

    Keyed by the site rather than by a person: somebody renaming a channel is
    naming the THING, not their view of it, and a figure handed to a colleague
    has to read the same for both of them. `set_by` is kept so a name that
    turns out to be wrong has somebody to ask.
    """

    __tablename__ = "channel_label"

    site: Mapped[str] = mapped_column(String(64), primary_key=True)
    feed: Mapped[str] = mapped_column(String(128), primary_key=True)
    channel: Mapped[str] = mapped_column(String(256), primary_key=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    set_by: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
