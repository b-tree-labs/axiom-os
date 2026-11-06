# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Initial schema for background tasks.

Revision ID: 0001
Revises:
Create Date: 2026-10-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

FALLBACK_SCHEMA = "tasks"  # ADR-052: never the public schema


def _schema() -> str:
    """The schema the migration environment targets (a test run may suffix it per worker), never a
    literal: a hardcoded name creates tables where ``session_for`` does not look."""
    return op.get_context().version_table_schema or FALLBACK_SCHEMA


JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    SCHEMA = _schema()
    op.create_table(
        "task",
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("node_id", sa.String(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("command", JSONType, nullable=False),
        sa.Column("cwd", sa.Text(), nullable=False),
        sa.Column("spawner_principal", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("output_path", sa.Text(), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.String(), nullable=True),
        sa.Column("ended_at", sa.String(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("task_id"),
        schema=SCHEMA,
    )
    op.create_index("ix_task_node_created", "task", ["node_id", "created_at"], schema=SCHEMA)
    op.create_index("ix_task_status", "task", ["status"], schema=SCHEMA)
    op.create_index("ix_task_principal", "task", ["spawner_principal"], schema=SCHEMA)


def downgrade() -> None:
    SCHEMA = _schema()
    op.drop_table("task", schema=SCHEMA)
