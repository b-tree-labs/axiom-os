# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Initial chat session store.

Sessions were JSON files under ``runtime/sessions/``, which cannot support a
second surface: every machine keeps its own copy, so a conversation started on
a laptop is invisible from a phone or a web harness. This table is the store
those surfaces share.

Revision ID: 0001
Revises:
Create Date: 2026-09-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "chat"


def upgrade() -> None:
    op.create_table(
        "chat_sessions",
        sa.Column("session_id", sa.String(64), primary_key=True),
        sa.Column("principal_id", sa.String(256), nullable=False, index=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "archived", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    # Resuming asks "the newest session for this principal", so the index
    # carries the sort column too and the answer never scans the principal's
    # whole history.
    op.create_index(
        "ix_chat_sessions_principal_updated",
        "chat_sessions",
        ["principal_id", "updated_at"],
        schema=SCHEMA,
    )

    # Messages are ROWS, not a field of the session.
    #
    # Holding them in the session's JSON meant rewriting the whole
    # conversation on every turn — O(n) per turn, O(n-squared) over a session,
    # measured at 108 KB per save by turn 200. Tolerable against local disk,
    # not against a network.
    #
    # `(session_id, seq)` is the primary key, so ordering is exact and two
    # surfaces appending the same position collide loudly rather than
    # silently interleaving a conversation. Wall clocks cannot do that job:
    # surfaces run on different machines and two turns can share a
    # millisecond.
    op.create_table(
        "chat_messages",
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey(f"{SCHEMA}.chat_sessions.session_id"),
            primary_key=True,
        ),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_calls", sa.JSON(), nullable=True),
        sa.Column("timestamp", sa.String(64), nullable=False),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("chat_messages", schema=SCHEMA)
    op.drop_index(
        "ix_chat_sessions_principal_updated", "chat_sessions", schema=SCHEMA
    )
    op.drop_table("chat_sessions", schema=SCHEMA)
