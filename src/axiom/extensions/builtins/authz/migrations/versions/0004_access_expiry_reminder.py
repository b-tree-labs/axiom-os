# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""When KEEP reminded the person who gave an access entry that it ends (SHR-R7.1).

One nullable column, ``reminded_at``, on ``access_grants``, so the week-ahead
reminder is sent once. Only adds (ADR-182).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("access_grants", sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True), schema="authz")


def downgrade() -> None:
    op.drop_column("access_grants", "reminded_at", schema="authz")
