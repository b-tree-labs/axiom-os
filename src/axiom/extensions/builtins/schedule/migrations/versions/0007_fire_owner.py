# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Record which process claimed each fire.

``owner_pid`` + ``owner_started_at`` name ONE process (the start time makes a
reused pid a different process). A restarting engine uses them to tell a fire
whose owner is dead (an orphan, recoverable at once) from one whose owner is a
live copy still working (left alone), with no signal from a supervisor.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("schedule_fire_log", sa.Column("owner_pid", sa.Integer(), nullable=True), schema="schedule")
    op.add_column("schedule_fire_log", sa.Column("owner_started_at", sa.Float(), nullable=True), schema="schedule")


def downgrade() -> None:
    op.drop_column("schedule_fire_log", "owner_started_at", schema="schedule")
    op.drop_column("schedule_fire_log", "owner_pid", schema="schedule")
