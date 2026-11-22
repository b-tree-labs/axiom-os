# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-213 D9: the export-control review trail (hold, release, keep-held, re-hold).

Expand only (ADR-182): one new table. Every op names the schema read from
``version_table_schema`` (see 0001).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _schema() -> str:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("library migrations need version_table_schema (ADR-052)")
    return schema


def upgrade() -> None:
    S = _schema()
    op.create_table(
        "library_ec_reviews",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("site", sa.String(64), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("item_id", sa.String(36), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("principal", sa.String(200), nullable=False),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column("code", sa.String(64), nullable=False, server_default=""),
        sa.Column("basis", sa.String(64), nullable=False, server_default=""),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("record", sa.JSON, nullable=False),
        sa.Column("signed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        schema=S,
    )
    op.create_index("ix_library_ec_reviews_item", "library_ec_reviews", ["item_id", "sha256"], schema=S)
    op.create_index("ux_library_ec_reviews_site_seq", "library_ec_reviews", ["site", "seq"], unique=True, schema=S)


def downgrade() -> None:
    S = _schema()
    op.drop_index("ux_library_ec_reviews_site_seq", table_name="library_ec_reviews", schema=S)
    op.drop_index("ix_library_ec_reviews_item", table_name="library_ec_reviews", schema=S)
    op.drop_table("library_ec_reviews", schema=S)
