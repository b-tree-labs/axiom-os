# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A stop's public message, stored apart from its internal reason (SW-A23).

Expand only (ADR-182): one nullable column on ``rollouts``. The release before this one
never reads it, so both run against one database during a switch.

Every op names ``schema=SCHEMA``: ``engine_for`` does not set the search path.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

SCHEMA = "flags"


def upgrade() -> None:
    op.add_column("rollouts", sa.Column("kill_message", sa.Text(), nullable=True), schema=SCHEMA)


def downgrade() -> None:
    op.drop_column("rollouts", "kill_message", schema=SCHEMA)
