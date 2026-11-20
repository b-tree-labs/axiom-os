# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every rollout mode: units, variants, external cohorts, stage plans, scheduled and approved
changes (ADR-257 D5, LOOP-F14 to F20, F22, F23).

Expand only (ADR-182): nullable columns on ``flag_overrides``, ``cohorts`` and ``rollouts``,
and two new tables. The release before this one never reads them, so both run against one
database during a switch. ``rollouts.bucket_by`` keeps its check (principal or account): any
other unit is written to the new ``bucket_unit`` column only.

Every op names ``schema=SCHEMA``: ``engine_for`` does not set the search path.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SCHEMA = "flags"

JSONDoc = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("flag_overrides", sa.Column("variant", sa.String(64), nullable=True), schema=SCHEMA)
    op.add_column("rollouts", sa.Column("bucket_unit", sa.String(255), nullable=True), schema=SCHEMA)
    op.add_column("cohorts", sa.Column("source", JSONDoc, nullable=True), schema=SCHEMA)
    op.add_column("cohorts", sa.Column("refresh_every_s", sa.Integer(), nullable=True), schema=SCHEMA)
    op.add_column("cohorts", sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True), schema=SCHEMA)
    op.add_column("cohorts", sa.Column("source_error", sa.Text(), nullable=True), schema=SCHEMA)
    op.create_table(
        "stage_plans",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("environment", sa.String(32), primary_key=True),
        sa.Column("plan", JSONDoc, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("stage", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage_entered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("halted_reason", sa.Text(), nullable=True),
        sa.Column("history", JSONDoc, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'halted', 'done', 'rolled_back')", name="ck_stage_plans_status"
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_stage_plans_environment", "stage_plans", ["environment"], schema=SCHEMA)
    op.create_table(
        "changes",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("capability", sa.String(64), nullable=False),
        sa.Column("params", JSONDoc, nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("environment", sa.String(32), nullable=True),
        sa.Column("author", sa.String(255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("policy", JSONDoc, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("approvals", JSONDoc, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("result", JSONDoc, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_index("ix_changes_plan_hash", "changes", ["plan_hash"], schema=SCHEMA)
    op.create_index("ix_changes_status_run_at", "changes", ["status", "run_at"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("ix_changes_status_run_at", table_name="changes", schema=SCHEMA)
    op.drop_index("ix_changes_plan_hash", table_name="changes", schema=SCHEMA)
    op.drop_table("changes", schema=SCHEMA)
    op.drop_index("ix_stage_plans_environment", table_name="stage_plans", schema=SCHEMA)
    op.drop_table("stage_plans", schema=SCHEMA)
    op.drop_column("cohorts", "source_error", schema=SCHEMA)
    op.drop_column("cohorts", "refreshed_at", schema=SCHEMA)
    op.drop_column("cohorts", "refresh_every_s", schema=SCHEMA)
    op.drop_column("cohorts", "source", schema=SCHEMA)
    op.drop_column("rollouts", "bucket_unit", schema=SCHEMA)
    op.drop_column("flag_overrides", "variant", schema=SCHEMA)
