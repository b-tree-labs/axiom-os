# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The reports and groups tables (LOOP-B1 to B5).

New tables only (expand, ADR-182). Every op names ``schema=SCHEMA``: ``engine_for`` does
not set the search path, so an unqualified op would write to ``public``.

Revision ID: 0001
Revises:
Create Date: 2026-11-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "feedback"
TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "groups",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("cause_key", sa.String(512), nullable=False, unique=True),
        sa.Column("release", sa.String(64), nullable=True),
        sa.Column("flag", sa.String(64), nullable=True),
        sa.Column("environment", sa.String(32), nullable=True),
        sa.Column("capability", sa.String(128), nullable=True),
        sa.Column("screen", sa.String(255), nullable=True),
        sa.Column("owner", sa.String(255), nullable=False),
        sa.Column("rollback", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=True),
        sa.Column("bad", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("good", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("severe", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("first_at", TZ, nullable=False),
        sa.Column("last_at", TZ, nullable=False),
        sa.Column("decided_by", sa.String(255), nullable=True),
        sa.Column("decided_at", TZ, nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("notices", postgresql.JSONB(), nullable=True),
        schema=SCHEMA,
    )
    op.create_index("ix_groups_flag", "groups", ["flag"], schema=SCHEMA)
    op.create_index("ix_groups_status", "groups", ["status"], schema=SCHEMA)
    op.create_table(
        "reports",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("reporter", sa.String(255), nullable=False),
        sa.Column("reporter_kind", sa.String(16), nullable=True),
        sa.Column("verdict", sa.String(8), nullable=False),
        sa.Column("severe", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("surface", sa.String(32), nullable=True),
        sa.Column("release", sa.String(64), nullable=True),
        sa.Column("node", sa.String(255), nullable=True),
        sa.Column("environment", sa.String(32), nullable=True),
        sa.Column("capability", sa.String(128), nullable=True),
        sa.Column("screen", sa.String(255), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("flags_on", postgresql.JSONB(), nullable=True),
        sa.Column("flag", sa.String(64), nullable=True),
        sa.Column("group_id", sa.String(32), nullable=False),
        sa.CheckConstraint("verdict IN ('good', 'bad')", name="ck_reports_verdict"),
        schema=SCHEMA,
    )
    op.create_index("ix_reports_created_at", "reports", ["created_at"], schema=SCHEMA)
    op.create_index("ix_reports_flag", "reports", ["flag"], schema=SCHEMA)
    op.create_index("ix_reports_group_id", "reports", ["group_id"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_table("reports", schema=SCHEMA)
    op.drop_table("groups", schema=SCHEMA)
