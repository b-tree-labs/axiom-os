# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Initial schema: signals, media, participants, people with pgvector.

Revision ID: 001
Revises:
Create Date: 2026-02-24

This migration creates the complete initial schema for Neut Sense:
- pgvector extension for vector similarity search
- signals: Signal chunks with embeddings for RAG
- media: Audio/video recordings with transcripts and embeddings
- participants: Links people to media with access control
- people: Person registry with aliases
- HNSW indexes for fast vector search
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = '001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Embedding dimension (OpenAI text-embedding-3-small)
EMBEDDING_DIM = 1536


def upgrade() -> None:
    """Create initial schema with pgvector support."""

    # Enable pgvector extension. IF NOT EXISTS is a no-op when already installed,
    # so this is safe inside a transaction. Callers should run
    # ensure_pgvector_extension() on a fresh database before migrating.
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))  # type: ignore[attr-defined]

    # =========================================================================
    # Signals table - Signal chunks with embeddings for RAG
    # =========================================================================
    op.create_table(
        "signals",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM)),
        sa.Column("signal_type", sa.Text),
        sa.Column("initiative", sa.Text),
        sa.Column("source", sa.Text),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True)),
        sa.Column("metadata", sa.JSON, server_default="{}"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("owner_id", sa.Text),
        sa.Column("version", sa.Integer, server_default="1"),
    )

    # =========================================================================
    # Media table - Recordings with transcripts and embeddings
    # =========================================================================
    op.create_table(
        "media",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("media_type", sa.Text),
        sa.Column("title", sa.Text),
        sa.Column("transcript", sa.Text),
        sa.Column("transcript_preview", sa.Text),
        sa.Column("embedding", Vector(EMBEDDING_DIM)),
        sa.Column("duration_sec", sa.Float),
        sa.Column("recorded_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("owner_id", sa.Text),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("version", sa.Integer, server_default="1"),
    )

    # =========================================================================
    # Participants table - Links people to media with access control
    # =========================================================================
    op.create_table(
        "participants",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("media_id", sa.Text, sa.ForeignKey("media.id", ondelete="CASCADE"), nullable=False),
        sa.Column("person_id", sa.Text, nullable=False),
        sa.Column("name", sa.Text),
        sa.Column("role", sa.Text),
        sa.Column("access_level", sa.Text, server_default="participant"),
        sa.Column("mention_count", sa.Integer, server_default="0"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
    )

    # =========================================================================
    # People table - Person registry with aliases
    # =========================================================================
    op.create_table(
        "people",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("aliases", sa.ARRAY(sa.Text)),
        sa.Column("email", sa.Text),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
    )

    # =========================================================================
    # HNSW indexes for fast vector search (cosine similarity)
    # =========================================================================
    op.create_index(
        "signals_embedding_idx",
        "signals",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "media_embedding_idx",
        "media",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    # =========================================================================
    # B-tree indexes for filtering
    # =========================================================================
    op.create_index("idx_signals_type", "signals", ["signal_type"])
    op.create_index("idx_signals_initiative", "signals", ["initiative"])
    op.create_index("idx_signals_timestamp", "signals", ["timestamp"])
    op.create_index("idx_media_owner", "media", ["owner_id"])
    op.create_index("idx_media_recorded", "media", ["recorded_at"])
    op.create_index("idx_participants_media", "participants", ["media_id"])
    op.create_index("idx_participants_person", "participants", ["person_id"])


def downgrade() -> None:
    """Drop all tables (destructive!)."""

    op.drop_index("idx_participants_person", table_name="participants")
    op.drop_index("idx_participants_media", table_name="participants")
    op.drop_index("idx_media_recorded", table_name="media")
    op.drop_index("idx_media_owner", table_name="media")
    op.drop_index("idx_signals_timestamp", table_name="signals")
    op.drop_index("idx_signals_initiative", table_name="signals")
    op.drop_index("idx_signals_type", table_name="signals")
    op.drop_index("media_embedding_idx", table_name="media")
    op.drop_index("signals_embedding_idx", table_name="signals")

    op.drop_table("participants")
    op.drop_table("people")
    op.drop_table("media")
    op.drop_table("signals")

    # Note: We don't drop the vector extension as other databases might use it
