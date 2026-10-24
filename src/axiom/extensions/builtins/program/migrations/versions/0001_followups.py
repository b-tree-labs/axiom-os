# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Follow-ups and their append-only journal (ADR-196).

Every op names ``schema=SCHEMA``: ``engine_for`` does not set ``search_path``,
so an unqualified op would write to ``public`` (guarded by
``tests/infra/test_migrations_stay_in_their_schema.py``). Expand-only: two new
tables, nothing dropped or renamed.

Revision ID: 0001
Revises:
Create Date: 2026-10-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "program"


def upgrade() -> None:
    op.create_table(
        "followup",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("what", sa.String(500), nullable=False),
        sa.Column("owner", sa.String(200), nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("original_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="open"),
        sa.Column("check_skill", sa.String(200), nullable=True),
        sa.Column("check_args", sa.JSON(), nullable=True),
        sa.Column("next_command", sa.String(500), nullable=True),
        sa.Column("ladder", sa.JSON(), nullable=False),
        sa.Column("cycle", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rung", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("schedule_id", sa.String(64), nullable=True),
        sa.Column("shared_with", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_by", sa.String(200), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.String(200), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_note", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('open','due','acknowledged','snoozed','done','expired')",
            name="ck_followup_state",
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_followup_owner", "followup", ["owner"], schema=SCHEMA)
    op.create_index("ix_followup_created_by", "followup", ["created_by"], schema=SCHEMA)
    op.create_index("ix_followup_state", "followup", ["state"], schema=SCHEMA)
    op.create_table(
        "followup_event",
        sa.Column("seq", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("followup_id", sa.String(32), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("cycle", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rung", sa.Integer(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("dedup_key", sa.String(200), nullable=True),
        sa.UniqueConstraint("dedup_key", name="uq_followup_event_dedup"),
        schema=SCHEMA,
    )
    op.create_index("ix_followup_event_followup_id", "followup_event", ["followup_id"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("ix_followup_event_followup_id", table_name="followup_event", schema=SCHEMA)
    op.drop_table("followup_event", schema=SCHEMA)
    op.drop_index("ix_followup_state", table_name="followup", schema=SCHEMA)
    op.drop_index("ix_followup_created_by", table_name="followup", schema=SCHEMA)
    op.drop_index("ix_followup_owner", table_name="followup", schema=SCHEMA)
    op.drop_table("followup", schema=SCHEMA)
