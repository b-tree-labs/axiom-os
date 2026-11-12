# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`stream` becomes `feed` in the site catalog projection.

The platform settled on one word for a group of channels from one producer on
2026-09-28, and the store's own column moved with it. This table projects that
catalogue for the web surface, so it moves too rather than becoming the last
place the old word survives.

A column rename in Postgres is a catalogue edit: no table rewrite, instant on
a table of any size, and the primary key follows the column by identity rather
than by name.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28
"""
from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA = "webapp"


def upgrade() -> None:
    op.alter_column(
        "site_catalog_channel", "stream", new_column_name="feed", schema=SCHEMA
    )


def downgrade() -> None:
    op.alter_column(
        "site_catalog_channel", "feed", new_column_name="stream", schema=SCHEMA
    )
