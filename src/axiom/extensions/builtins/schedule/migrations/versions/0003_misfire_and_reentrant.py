# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Add restart-safety columns to ``schedule_definition``.

- ``misfire_policy`` — how to treat instants missed while the engine was down.
- ``reentrant`` — whether the action is safe to re-run after an interrupted fire
  (drives startup reconciliation).

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "schedule_definition",
        sa.Column(
            "misfire_policy",
            sa.String(),
            nullable=False,
            server_default="fire_once",
        ),
    )
    op.add_column(
        "schedule_definition",
        sa.Column(
            "reentrant",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("schedule_definition", "reentrant")
    op.drop_column("schedule_definition", "misfire_policy")
