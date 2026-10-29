# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Initial schema for the library: the catalog of filed documents and its events.

Every op names the schema, read from ``version_table_schema`` (the attest
pattern): ``engine_for`` does not set ``search_path``, so an unqualified op
writes to ``public`` silently, and reading it rather than naming it lets the
same revision provision a worker-scoped test schema.

Revision ID: 0001
Revises:
Create Date: 2026-10-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None



def _schema() -> str:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("library migrations need version_table_schema (ADR-052)")
    return schema


def upgrade() -> None:
    SCHEMA = _schema()
    op.create_table(
        "library_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("site", sa.String(64), nullable=False),
        sa.Column("owner", sa.String(200), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False, server_default="site"),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("size", sa.BigInteger, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("scan", sa.String(16), nullable=False, server_default="not-scanned"),
        sa.Column("taxonomy", sa.String(64), nullable=False, server_default=""),
        sa.Column("document_type", sa.String(64), nullable=False, server_default="other"),
        sa.Column("category", sa.String(64), nullable=False, server_default="documents"),
        sa.Column("label", sa.String(128), nullable=False, server_default=""),
        sa.Column("confidence", sa.Float, nullable=False, server_default="0"),
        sa.Column("classifier", sa.String(64), nullable=False, server_default="fallback"),
        sa.Column("attributes", sa.JSON, nullable=False),
        sa.Column("tags", sa.JSON, nullable=False),
        sa.Column("collection", sa.String(128), nullable=False, server_default=""),
        sa.Column("bronze_record", sa.Text, nullable=False, server_default=""),
        sa.Column("bronze_content", sa.Text, nullable=False, server_default=""),
        sa.Column("corpus", sa.String(200), nullable=False, server_default=""),
        sa.Column("source_path", sa.Text, nullable=False, server_default=""),
        sa.Column("index_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("index_reason", sa.Text, nullable=False, server_default=""),
        sa.Column("chunk_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.String(200), nullable=False, server_default=""),
        schema=SCHEMA,
    )
    op.create_index("ix_library_items_site_live", "library_items", ["site", "deleted_at"], schema=SCHEMA)
    op.create_index("ix_library_items_site_sha", "library_items", ["site", "sha256"], schema=SCHEMA)

    op.create_table(
        "library_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.String(36), nullable=False),
        sa.Column("site", sa.String(64), nullable=False),
        sa.Column("principal", sa.String(200), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("surface", sa.String(32), nullable=False, server_default=""),
        sa.Column("detail", sa.JSON, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_index("ix_library_events_item", "library_events", ["item_id"], schema=SCHEMA)


def downgrade() -> None:
    SCHEMA = _schema()
    op.drop_index("ix_library_events_item", table_name="library_events", schema=SCHEMA)
    op.drop_table("library_events", schema=SCHEMA)
    op.drop_index("ix_library_items_site_sha", table_name="library_items", schema=SCHEMA)
    op.drop_index("ix_library_items_site_live", table_name="library_items", schema=SCHEMA)
    op.drop_table("library_items", schema=SCHEMA)
