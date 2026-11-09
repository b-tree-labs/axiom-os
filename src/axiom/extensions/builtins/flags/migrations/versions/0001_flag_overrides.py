# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The flag_overrides table (ADR-210).

Every op names ``schema=SCHEMA``: ``engine_for`` does not set the search
path, so an unqualified op would write to ``public``.

Revision ID: 0001
Revises:
Create Date: 2026-10-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "flags"


def upgrade() -> None:
    op.create_table(
        "flag_overrides",
        sa.Column("scope", sa.String(16), primary_key=True),
        sa.Column("scope_id", sa.String(255), primary_key=True),
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.String(8), nullable=False),
        sa.Column("updated_by", sa.String(255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("scope IN ('node', 'account', 'user')", name="ck_flag_overrides_scope"),
        sa.CheckConstraint("value IN ('on', 'off', 'soon')", name="ck_flag_overrides_value"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_flag_overrides_key", "flag_overrides", ["key"], schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_index("ix_flag_overrides_key", table_name="flag_overrides", schema=SCHEMA)
    op.drop_table("flag_overrides", schema=SCHEMA)
