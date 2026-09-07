# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Add anchor columns to ``schedule_definition``.

A cadence can be anchored to a time-slot's actual time + an offset; it sits
dormant until the actual is recorded, then fires relative to it (the anchor
pattern — e.g. "open a window 24h after the actual end").

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "schedule_definition",
        sa.Column("anchor_time_slot_id", sa.String(), nullable=True, index=True),
    )
    op.add_column(
        "schedule_definition",
        sa.Column("anchor_to", sa.String(), nullable=True),
    )
    op.add_column(
        "schedule_definition",
        sa.Column(
            "anchor_offset_seconds", sa.Integer(), nullable=False, server_default="0"
        ),
    )


def downgrade() -> None:
    op.drop_column("schedule_definition", "anchor_offset_seconds")
    op.drop_column("schedule_definition", "anchor_to")
    op.drop_column("schedule_definition", "anchor_time_slot_id")
