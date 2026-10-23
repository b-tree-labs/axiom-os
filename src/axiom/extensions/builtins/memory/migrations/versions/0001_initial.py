# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Initial schema for the memory layer: the artifact ledger and the concept graph.

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

FALLBACK_SCHEMA = "memory"  # ADR-052: never the public schema


def _schema() -> str:
    """The schema the migration environment targets (``engine_for``'s answer, which a test run
    may suffix per worker), never a literal: a hardcoded name creates tables where ``session_for``
    does not look."""
    return op.get_context().version_table_schema or FALLBACK_SCHEMA
JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    SCHEMA = _schema()
    op.create_table(
        "artifact",
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("data", JSONType, nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.Float(53), nullable=False),
        sa.Column("signature", sa.LargeBinary(), nullable=True),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deletion_reason", sa.Text(), nullable=True),
        sa.Column("metadata", JSONType, nullable=False, server_default=sa.text("'{}'")),
        sa.PrimaryKeyConstraint("scope", "id"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_artifact_kind_name",
        "artifact",
        ["scope", "kind", "name", "created_at", "seq"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_artifact_created", "artifact", ["scope", "created_at", "seq"], schema=SCHEMA
    )
    if op.get_bind().dialect.name == "postgresql":
        # The projection fast path filters fragments by cognitive type and principal.
        op.execute(
            f"CREATE INDEX ix_artifact_fragment ON {SCHEMA}.artifact "
            "(scope, (data->>'cognitive_type'), ((data->'provenance')->>'principal_id')) "
            "WHERE kind = 'fragment' AND NOT deleted"
        )

    op.create_table(
        "concept",
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("concept_id", sa.String(), nullable=False),
        sa.Column("canonical_name", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(53), nullable=False, server_default="1.0"),
        sa.PrimaryKeyConstraint("scope", "concept_id"),
        schema=SCHEMA,
    )
    op.create_index("ix_concept_name", "concept", ["scope", "canonical_name"], schema=SCHEMA)
    op.create_table(
        "concept_extracted_from",
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("concept_id", sa.String(), nullable=False),
        sa.Column("fragment_id", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("scope", "concept_id", "fragment_id"),
        schema=SCHEMA,
    )
    op.create_table(
        "concept_edge",
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("from_concept", sa.String(), nullable=False),
        sa.Column("to_concept", sa.String(), nullable=False),
        sa.Column("edge_type", sa.String(), nullable=False),
        sa.Column("weight", sa.Float(53), nullable=False, server_default="1.0"),
        sa.PrimaryKeyConstraint("scope", "from_concept", "to_concept", "edge_type"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_edge_from", "concept_edge", ["scope", "from_concept", "edge_type"], schema=SCHEMA
    )
    op.create_index(
        "ix_edge_to", "concept_edge", ["scope", "to_concept", "edge_type"], schema=SCHEMA
    )
    op.create_table(
        "concept_edge_evidence",
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("from_concept", sa.String(), nullable=False),
        sa.Column("to_concept", sa.String(), nullable=False),
        sa.Column("edge_type", sa.String(), nullable=False),
        sa.Column("fragment_id", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("scope", "from_concept", "to_concept", "edge_type", "fragment_id"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    SCHEMA = _schema()
    for table in (
        "concept_edge_evidence",
        "concept_edge",
        "concept_extracted_from",
        "concept",
        "artifact",
    ):
        op.drop_table(table, schema=SCHEMA)
