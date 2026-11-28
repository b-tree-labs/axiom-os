# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Per-principal feedback on a message.

A training signal kept out of the append-only transcript, so a rating (which can
change, and is per-principal) never rewrites a message row. Message-grained and
principal-attributed — complementary to the RAG interaction_log, which is
query-grained.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SCHEMA = "chat"


def upgrade() -> None:
    op.create_table(
        "chat_message_feedback",
        sa.Column("session_id", sa.String(64), primary_key=True),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("principal_id", sa.String(256), primary_key=True),
        sa.Column("rating", sa.String(32), nullable=False, server_default=""),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id", "seq"],
            [f"{SCHEMA}.chat_messages.session_id", f"{SCHEMA}.chat_messages.seq"],
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("chat_message_feedback", schema=SCHEMA)
