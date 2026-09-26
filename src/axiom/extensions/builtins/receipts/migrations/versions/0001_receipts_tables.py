# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Initial schema for the receipts extension: the focus directive and
the brief snapshot (the delta baseline).

Every op names ``schema=SCHEMA`` explicitly: ``engine_for`` does not set
``search_path``, so an unqualified op writes to ``public`` silently (the
class ``test_migrations_stay_in_their_schema`` guards).

Revision ID: 0001
Revises:
Create Date: 2026-09-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "receipts"


def upgrade() -> None:
    op.create_table(
        "focus",
        sa.Column("site", sa.String(200), primary_key=True),
        sa.Column("text", sa.String(2000), nullable=False),
        sa.Column("set_by", sa.String(200), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="active"),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "brief_snapshot",
        sa.Column("site", sa.String(200), primary_key=True),
        sa.Column("entity_kind", sa.String(40), primary_key=True),
        sa.Column("entity_id", sa.String(200), primary_key=True),
        sa.Column("claim_kind", sa.String(80), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("brief_snapshot", schema=SCHEMA)
    op.drop_table("focus", schema=SCHEMA)
