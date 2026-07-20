# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Add conflict + operator-veto columns to ``schedule_time_slot``.

- ``resource_key`` — the scarce resource a slot reserves; overlapping slots on
  the same key conflict.
- ``fixed`` / ``priority`` — immovable + preemption ordering for conflicts.
- ``proposed_planned_start`` / ``proposed_planned_end`` — a pending reschedule
  awaiting operator confirmation.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("schedule_time_slot", sa.Column("resource_key", sa.String(), nullable=True, index=True))
    op.add_column("schedule_time_slot", sa.Column("fixed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("schedule_time_slot", sa.Column("priority", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("schedule_time_slot", sa.Column("proposed_planned_start", sa.DateTime(timezone=True), nullable=True))
    op.add_column("schedule_time_slot", sa.Column("proposed_planned_end", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("schedule_time_slot", "proposed_planned_end")
    op.drop_column("schedule_time_slot", "proposed_planned_start")
    op.drop_column("schedule_time_slot", "priority")
    op.drop_column("schedule_time_slot", "fixed")
    op.drop_column("schedule_time_slot", "resource_key")
