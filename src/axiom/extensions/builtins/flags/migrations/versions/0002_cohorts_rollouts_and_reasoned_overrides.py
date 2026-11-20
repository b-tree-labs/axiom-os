# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Cohorts, rollouts and reasoned overrides (ADR-257 D5, LOOP-F1/F4/F6/F10).

Expand only (ADR-182): two new tables beside ``flag_overrides``, and two nullable
columns on it. The release before this one never reads them, so both run against
one database during a switch.

Every op names ``schema=SCHEMA``: ``engine_for`` does not set the search path.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA = "flags"

JSONDoc = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("flag_overrides", sa.Column("reason", sa.Text(), nullable=True), schema=SCHEMA)
    op.add_column(
        "flag_overrides", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True), schema=SCHEMA
    )
    op.create_table(
        "cohorts",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("rules", JSONDoc, nullable=True),
        sa.Column("principals", JSONDoc, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("accounts", JSONDoc, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "rollouts",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("environment", sa.String(32), primary_key=True),
        sa.Column("rules", JSONDoc, nullable=True),
        sa.Column("bucket_by", sa.String(16), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("kill_switch", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("kill_reason", sa.Text(), nullable=True),
        sa.Column("kill_by", sa.String(255), nullable=True),
        sa.Column("kill_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.String(255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "bucket_by IS NULL OR bucket_by IN ('principal', 'account')", name="ck_rollouts_bucket_by"
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_rollouts_environment", "rollouts", ["environment"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("ix_rollouts_environment", table_name="rollouts", schema=SCHEMA)
    op.drop_table("rollouts", schema=SCHEMA)
    op.drop_table("cohorts", schema=SCHEMA)
    op.drop_column("flag_overrides", "expires_at", schema=SCHEMA)
    op.drop_column("flag_overrides", "reason", schema=SCHEMA)
