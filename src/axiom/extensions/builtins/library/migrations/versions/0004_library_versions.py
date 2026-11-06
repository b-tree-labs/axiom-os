# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Item versions: each keeps its own bronze original and its own pipeline run.

Expand only (ADR-182): one new table. Every op names the schema read from
``version_table_schema`` (see 0001).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
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
        "library_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("site", sa.String(64), nullable=False),
        sa.Column("item_id", sa.String(36), nullable=False),
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("size", sa.BigInteger, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("scan", sa.String(16), nullable=False, server_default="not-scanned"),
        sa.Column("bronze_content", sa.Text, nullable=False, server_default=""),
        sa.Column("bronze_record", sa.Text, nullable=False, server_default=""),
        sa.Column("job_id", sa.String(36), nullable=False, server_default=""),
        sa.Column("source", sa.String(16), nullable=False, server_default="upload"),
        sa.Column("restored_from", sa.Integer, nullable=True),
        sa.Column("created_by", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=S,
    )
    op.create_index(
        "ux_library_versions_item_number",
        "library_versions",
        ["item_id", "number"],
        unique=True,
        schema=S,
    )


def downgrade() -> None:
    S = _schema()
    op.drop_index("ux_library_versions_item_number", table_name="library_versions", schema=S)
    op.drop_table("library_versions", schema=S)
