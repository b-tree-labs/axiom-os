# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Initial webapp schema — the serving catalog.

Revision ID: 0001
Revises:
Create Date: 2026-09-16

The catalog shipped with `Base.metadata.create_all` and no migration, which
works exactly until the table needs to change: create_all adds what is missing
and is silent about what already exists and differs. A column added to the model
would simply never appear on a node that already had the table, and the mismatch
would surface as a query failing on a column the code is certain it declared.

Every other extension in this package versions its schema. This one now does
too.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "webapp"


def upgrade() -> None:
    op.create_table(
        "site_catalog_channel",
        sa.Column("site", sa.String(64), primary_key=True),
        sa.Column("stream", sa.String(128), primary_key=True),
        sa.Column("channel", sa.String(256), primary_key=True),
        sa.Column("unit", sa.String(64), nullable=False, server_default=""),
        sa.Column("rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_ts", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_ts", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        schema=SCHEMA,
    )
    # The listing reads by site, and the scope filter reads the distinct sites.
    # Without this every /api/v1/sites call scans a table that grows with every
    # channel at every site on the platform.
    op.create_index(
        "ix_site_catalog_channel_site",
        "site_catalog_channel",
        ["site"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_site_catalog_channel_site", table_name="site_catalog_channel", schema=SCHEMA
    )
    op.drop_table("site_catalog_channel", schema=SCHEMA)
