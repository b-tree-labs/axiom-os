# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-213: the upload pipeline's jobs, sessions, extractions and routes, and the Data Book.

Expand only (ADR-182): new tables, and nullable columns on ``library_items``.
Every op names the schema read from ``version_table_schema`` (see 0001).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _schema() -> str:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("library migrations need version_table_schema (ADR-052)")
    return schema


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    S = _schema()
    for name, col in (
        ("folder", sa.Column("folder", sa.String(255), nullable=True)),
        ("placement", sa.Column("placement", sa.JSON, nullable=True)),
        ("pipeline_state", sa.Column("pipeline_state", sa.String(16), nullable=True)),
        ("job_id", sa.Column("job_id", sa.String(36), nullable=True)),
        ("ec", sa.Column("ec", sa.JSON, nullable=True)),
    ):
        op.add_column("library_items", col, schema=S)
    op.create_index("ix_library_items_site_folder", "library_items", ["site", "folder"], schema=S)

    op.create_table(
        "library_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("site", sa.String(64), nullable=False),
        sa.Column("owner", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="upload"),
        sa.Column("item_id", sa.String(36), nullable=True),
        sa.Column("upload_id", sa.String(36), nullable=True),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("size", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("bytes_received", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("state", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("stage", sa.String(16), nullable=False, server_default="received"),
        sa.Column("stages", sa.JSON, nullable=False),
        sa.Column("progress", sa.Float, nullable=False, server_default="0"),
        sa.Column("error", sa.JSON, nullable=True),
        sa.Column("options", sa.JSON, nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        schema=S,
    )
    op.create_index("ix_library_jobs_owner", "library_jobs", ["site", "owner", "updated_at"], schema=S)

    op.create_table(
        "library_uploads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("site", sa.String(64), nullable=False),
        sa.Column("owner", sa.String(200), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("size", sa.BigInteger, nullable=False),
        sa.Column("offset", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("declared_sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("staged_path", sa.Text, nullable=False),
        sa.Column("options", sa.JSON, nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="open"),
        sa.Column("chunk_size", sa.Integer, nullable=False, server_default=str(8 * 1024 * 1024)),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("expires_at", nullable=True),
        schema=S,
    )
    op.create_index("ix_library_uploads_owner", "library_uploads", ["site", "owner", "state"], schema=S)

    op.create_table(
        "library_extractions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.String(36), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("extractor", sa.String(128), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False, server_default="core"),
        sa.Column("contributed_by", sa.String(128), nullable=False, server_default=""),
        sa.Column("member", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("result", sa.JSON, nullable=False),
        sa.Column("tags", sa.JSON, nullable=False),
        sa.Column("derived_path", sa.Text, nullable=False, server_default=""),
        sa.Column("input_sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("output_sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("duration_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text, nullable=False, server_default=""),
        sa.Column("remedy", sa.Text, nullable=False, server_default=""),
        sa.Column("current", sa.Boolean, nullable=False, server_default=sa.true()),
        _ts("started_at"),
        schema=S,
    )
    op.create_index("ix_library_extractions_item", "library_extractions", ["item_id", "current"], schema=S)

    op.create_table(
        "library_routes",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.String(36), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("extractor", sa.String(128), nullable=False, server_default=""),
        sa.Column("tag", sa.String(32), nullable=False),
        sa.Column("route", sa.String(128), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("target", sa.String(255), nullable=False, server_default=""),
        sa.Column("detail", sa.Text, nullable=False, server_default=""),
        sa.Column("link", sa.JSON, nullable=True),
        sa.Column("remedy", sa.Text, nullable=False, server_default=""),
        sa.Column("current", sa.Boolean, nullable=False, server_default=sa.true()),
        _ts("at"),
        schema=S,
    )
    op.create_index("ix_library_routes_item", "library_routes", ["item_id", "current"], schema=S)

    op.create_table(
        "library_folders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("site", sa.String(64), nullable=False),
        sa.Column("path", sa.String(255), nullable=False),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="user"),
        sa.Column("shown_under", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(200), nullable=False, server_default=""),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("deleted_at", nullable=True),
        schema=S,
    )
    op.create_index("ix_library_folders_site", "library_folders", ["site", "deleted_at"], schema=S)

    op.create_table(
        "library_corrections",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("site", sa.String(64), nullable=False),
        sa.Column("item_id", sa.String(36), nullable=False),
        sa.Column("principal", sa.String(200), nullable=False),
        sa.Column("from_path", sa.String(255), nullable=False, server_default=""),
        sa.Column("to_path", sa.String(255), nullable=False),
        sa.Column("signature", sa.JSON, nullable=False),
        _ts("at"),
        schema=S,
    )
    op.create_index("ix_library_corrections_site", "library_corrections", ["site"], schema=S)


def downgrade() -> None:
    S = _schema()
    for table, index in (
        ("library_corrections", "ix_library_corrections_site"),
        ("library_folders", "ix_library_folders_site"),
        ("library_routes", "ix_library_routes_item"),
        ("library_extractions", "ix_library_extractions_item"),
        ("library_uploads", "ix_library_uploads_owner"),
        ("library_jobs", "ix_library_jobs_owner"),
    ):
        op.drop_index(index, table_name=table, schema=S)
        op.drop_table(table, schema=S)
    op.drop_index("ix_library_items_site_folder", table_name="library_items", schema=S)
    for name in ("ec", "job_id", "pipeline_state", "placement", "folder"):
        op.drop_column("library_items", name, schema=S)
