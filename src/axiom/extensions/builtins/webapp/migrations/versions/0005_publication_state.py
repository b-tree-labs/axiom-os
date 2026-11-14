# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The publication ledger: which gold object and gold version each published table came from.

MVP-11-A4/A5 (W13). One row per table the app database receives from gold.
Expand only (ADR-182): a new table; nothing existing changes.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-31
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

SCHEMA = "webapp"


def upgrade() -> None:
    op.create_table(
        "publication_state",
        sa.Column("app_table", sa.String(128), primary_key=True),
        sa.Column("gold_object", sa.String(256), nullable=False),
        sa.Column("gold_version", sa.Text(), nullable=False, server_default=""),
        sa.Column("version_kind", sa.String(16), nullable=False, server_default="watermark"),
        sa.Column("rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("freshness_s", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("invalidate_on", sa.String(16), nullable=False, server_default="both"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("publication_state", schema=SCHEMA)
