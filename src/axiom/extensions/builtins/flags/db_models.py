# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The flags schema: one table, flag_overrides (ADR-210, ADR-052).

No schema= on the table: session_for("flags") sets the search path, and
the migration names the schema on every op.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EXTENSION_SCHEMA = "flags"

#: The scopes an override is stored under, in resolution order (later wins).
SCOPES: tuple[str, ...] = ("node", "account", "user")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class FlagOverride(Base):
    """One override: this flag has this value for this node, account or user."""

    __tablename__ = "flag_overrides"

    scope: Mapped[str] = mapped_column(String(16), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(8), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
