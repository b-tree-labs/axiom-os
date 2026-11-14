# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Initial schema for the classroom search index: chunks (full-text), entities, edges.

Revision ID: 0001
Revises:
Create Date: 2026-10-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

FALLBACK_SCHEMA = "classroom"  # ADR-052: never the public schema


def _schema() -> str:
    """The schema the migration environment targets (a test run may suffix it per worker), never a
    literal: a hardcoded name creates tables where ``session_for`` does not look."""
    return op.get_context().version_table_schema or FALLBACK_SCHEMA


PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    SCHEMA = _schema()
    op.create_table(
        "index_chunk",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("file_id", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        schema=SCHEMA,
    )
    op.create_index("ix_index_chunk_scope_file", "index_chunk", ["scope", "file_id"], schema=SCHEMA)
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            f"CREATE INDEX ix_index_chunk_fts ON {SCHEMA}.index_chunk "
            "USING gin (to_tsvector('english', text))"
        )
    op.create_table(
        "index_entity",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("file_id", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("properties", JSONType, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("confidence", sa.Float(53), nullable=False, server_default="1.0"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_index_entity_scope_file", "index_entity", ["scope", "file_id"], schema=SCHEMA
    )
    op.create_index("ix_index_entity_name", "index_entity", ["scope", "name"], schema=SCHEMA)
    op.create_table(
        "index_edge",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("file_id", sa.String(), nullable=False),
        sa.Column("rel_type", sa.String(), nullable=False),
        sa.Column("from_name", sa.Text(), nullable=False),
        sa.Column("from_label", sa.String(), nullable=False),
        sa.Column("to_name", sa.Text(), nullable=False),
        sa.Column("to_label", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(53), nullable=False, server_default="1.0"),
        schema=SCHEMA,
    )
    op.create_index("ix_index_edge_scope_file", "index_edge", ["scope", "file_id"], schema=SCHEMA)


def downgrade() -> None:
    SCHEMA = _schema()
    for table in ("index_edge", "index_entity", "index_chunk"):
        op.drop_table(table, schema=SCHEMA)
