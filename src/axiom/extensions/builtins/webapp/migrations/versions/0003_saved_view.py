# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What somebody had configured on a surface, kept for when they come back.

A surface a person configures and loses is a surface they configure once and
then stop using.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SCHEMA = "webapp"


def upgrade() -> None:
    op.create_table(
        "saved_view",
        sa.Column("principal", sa.String(256), primary_key=True),
        sa.Column("surface", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), primary_key=True),
        # JSON as TEXT: the platform never queries inside it, so a JSON type
        # buys nothing and ties the table to one database. The same table has
        # to work on the SQLite a developer runs and the Postgres a node runs.
        sa.Column("document", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        schema=SCHEMA,
    )
    # The listing a surface opens with: this person's views of this surface.
    op.create_index(
        "saved_view_principal_surface",
        "saved_view",
        ["principal", "surface"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("saved_view_principal_surface", "saved_view", schema=SCHEMA)
    op.drop_table("saved_view", schema=SCHEMA)
