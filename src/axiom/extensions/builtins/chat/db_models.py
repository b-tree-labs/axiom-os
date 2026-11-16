# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chat session storage (ADR-052: schema-per-extension).

Sessions were JSON files under ``runtime/sessions/``, which is the one shape
that cannot support a second surface: every machine has its own copy, so a
conversation started on a laptop is unreachable from a phone or a web
harness. The file store stays for local testing; this is the store a hosted
session plane uses.

Owned rows: ``principal_id`` is the ADR-020 handle the session belongs to and
is indexed, because "where was I?" — the question every surface asks on
resume — is a lookup by principal.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: The schema this extension's tables live in. `session_for("chat")` sets
#: `search_path`, so table names here stay unqualified per ADR-052 — never
#: hardcode `schema=` on a table.
EXTENSION_SCHEMA = "chat"


class Base(DeclarativeBase):
    """Declarative base discovered by ``extensions_with_storage()``."""


class ChatSession(Base):
    """One chat session, portable across every surface that can reach the DB."""

    __tablename__ = "chat_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    principal_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    """ADR-020 handle of the owner. Empty for sessions written before
    ownership existed; those stay readable and are adopted on read."""
    site_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    """Physical install this session belongs to (ADR-050 site). Empty = unscoped
    (the CLI / single-install case). Reads on a shared surface filter by it."""
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False, default="", index=True)
    """Data-owner the web filters by (ADR-052 §D4 row-level tenancy). appkit's
    ``account_id`` maps to this at the API boundary. Empty = unscoped."""
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    starred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """User pin. A column (not payload) so listing can filter/sort on it and a
    star survives an append-save — see DatabaseSessionStore.set_starred."""
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    """Session-level state ONLY — context, usage, timestamps. NOT the
    messages; those are rows in `chat_messages`.

    Keeping messages here meant rewriting the whole conversation on every
    turn: O(n) per turn and O(n-squared) over a session. Measured at 108 KB
    per save by turn 200 — invisible on local disk, but this store is over a
    network."""
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class ChatMessage(Base):
    """One turn. Append-only.

    Rows are never rewritten, so saving turn N costs one INSERT rather than a
    rewrite of turns 1..N.
    """

    __tablename__ = "chat_messages"

    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("chat_sessions.session_id"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    """Position in the conversation, 0-based.

    Ordering comes from this and NOT from a timestamp. Surfaces run on
    different machines, so wall clocks disagree and two turns can share a
    millisecond; a sequence is exact and survives clock skew. It is part of
    the primary key, so two surfaces appending the same position collide
    loudly instead of silently interleaving a conversation.
    """
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    timestamp: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    """The turn's own ISO-8601 stamp, preserved as provenance. Read for
    display, never for ordering — see `seq`."""


class ChatMessageFeedback(Base):
    """One principal's feedback on one message. Training signal, kept out of the
    append-only transcript so a rating (which can change, and is per-principal)
    never rewrites a message row. Complementary to the RAG interaction_log, which
    is query-grained; this is message-grained and principal-attributed."""

    __tablename__ = "chat_message_feedback"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    principal_id: Mapped[str] = mapped_column(String(256), primary_key=True, default="")
    """Who gave it — attribution matters for a training signal on a shared store."""
    rating: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "seq"],
            ["chat_messages.session_id", "chat_messages.seq"],
        ),
    )


#: Resume is "this principal's most recent", so the index carries both.
Index(
    "ix_chat_sessions_principal_updated",
    ChatSession.principal_id,
    ChatSession.updated_at.desc(),
)

#: A shared surface lists "this site+tenant's most recent", so the scoped-read
#: index carries the scope axes and the sort column together.
Index(
    "ix_chat_sessions_scope_updated",
    ChatSession.site_id,
    ChatSession.tenant_id,
    ChatSession.updated_at.desc(),
)


__all__ = ["EXTENSION_SCHEMA", "Base", "ChatSession", "ChatMessage", "ChatMessageFeedback"]
