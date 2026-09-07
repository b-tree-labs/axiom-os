# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Add ``schedule_time_slot`` — the consumer-seam time reservation table.

Planned-vs-actual time windows with opaque consumer metadata, optionally linked
to a cadence. Owned by the scheduling seam (register_time_slot / record_actual /
time_slot_status).

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "schedule_time_slot",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("planned_start", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("planned_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("time_slot_metadata", sa.JSON(), nullable=True),
        sa.Column("schedule_id", sa.String(), nullable=True, index=True),
        sa.Column(
            "state", sa.String(), nullable=False, server_default="reserved", index=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("schedule_time_slot")
