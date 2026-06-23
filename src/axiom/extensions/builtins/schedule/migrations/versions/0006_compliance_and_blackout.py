# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Add compliance-window columns + the ``schedule_blackout`` table.

- ``compliance_window_seconds`` / ``compliance_action`` on schedule_definition —
  a fire later than the window is a compliance violation (``out_of_window``).
- ``schedule_blackout`` — windows during which fires are suppressed.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "schedule_definition",
        sa.Column("compliance_window_seconds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "schedule_definition",
        sa.Column("compliance_action", sa.String(), nullable=False, server_default="flag"),
    )
    op.create_table(
        "schedule_blackout",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("resource_key", sa.String(), nullable=True, index=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("schedule_blackout")
    op.drop_column("schedule_definition", "compliance_action")
    op.drop_column("schedule_definition", "compliance_window_seconds")
