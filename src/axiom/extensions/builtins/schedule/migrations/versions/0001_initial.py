# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Initial schema for the schedule extension.

Three tables per spec-axiom-schedule §2.3:

- ``schedule_definition`` — registered schedules
- ``schedule_fire_log`` — idempotency window log + dead-letter trail
- ``schedule_lease`` — singleton leader lease

Revision ID: 0001
Revises:
Create Date: 2026-05-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "schedule_definition",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("extension", sa.String(), nullable=True, index=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("cadence_kind", sa.String(), nullable=False),
        sa.Column("cadence_payload", sa.JSON(), nullable=False),
        sa.Column(
            "next_fire_at", sa.DateTime(timezone=True), nullable=True, index=True
        ),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "randomized_delay_seconds",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("classification_ceiling", sa.String(), nullable=True),
        sa.Column(
            "raci_default",
            sa.String(),
            nullable=False,
            server_default="autonomous",
        ),
        sa.Column("retry_policy", sa.JSON(), nullable=False),
        sa.Column("capability_envelope", sa.JSON(), nullable=True),
        sa.Column(
            "state", sa.String(), nullable=False, server_default="active", index=True
        ),
        sa.Column("paused_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema="schedule",
    )
    op.create_table(
        "schedule_fire_log",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("schedule_id", sa.String(), nullable=False, index=True),
        sa.Column("fire_time_bucket", sa.BigInteger(), nullable=False),
        sa.Column("params_hash", sa.String(), nullable=False),
        sa.Column(
            "intended_fire_at",
            sa.DateTime(timezone=True),
            nullable=False,
            index=True,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "outcome",
            sa.String(),
            nullable=False,
            server_default="pending",
            index=True,
        ),
        sa.Column("receipt_fragment_id", sa.String(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "schedule_id",
            "fire_time_bucket",
            "params_hash",
            name="uq_schedule_fire_log_idempotency",
        ),
        schema="schedule",
    )
    op.create_table(
        "schedule_lease",
        sa.Column(
            "singleton",
            sa.Boolean(),
            primary_key=True,
            server_default=sa.text("TRUE"),
        ),
        sa.Column("node_id", sa.String(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "singleton IS TRUE", name="ck_schedule_lease_singleton"
        ),
        schema="schedule",
    )


def downgrade() -> None:
    op.drop_table("schedule_lease", schema="schedule")
    op.drop_table("schedule_fire_log", schema="schedule")
    op.drop_table("schedule_definition", schema="schedule")
