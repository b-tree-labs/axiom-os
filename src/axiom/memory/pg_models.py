# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SQLAlchemy models for the memory layer's Postgres schema (ADR-052, ADR-174).

One ``memory`` schema holds the artifact ledger and the concept graph for
every scope. A *scope* replaces the one-file-per-scope layout of the SQLite
backends: the user's own memory, a runtime extension scope, a classroom, an
evaluation run. Every primary key starts with ``scope``, so scopes never
collide and a scope's rows are one index range.

Tables are schema-unqualified; ``session_for('memory')`` sets the search path.
JSON columns are JSONB on Postgres and plain JSON elsewhere (the test seam).
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Float,
    Index,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EXTENSION_SCHEMA = "memory"

JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class ArtifactRow(Base):
    __tablename__ = "artifact"

    scope: Mapped[str] = mapped_column(String, primary_key=True)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    # Insertion order, to break created_at ties the way the SQLite rowid did.
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    data: Mapped[dict] = mapped_column(JSONType, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[float] = mapped_column(Float(53), nullable=False)
    signature: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deletion_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict] = mapped_column("metadata", JSONType, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_artifact_kind_name", "scope", "kind", "name", "created_at", "seq"),
        Index("ix_artifact_created", "scope", "created_at", "seq"),
    )


class ConceptRow(Base):
    __tablename__ = "concept"

    scope: Mapped[str] = mapped_column(String, primary_key=True)
    concept_id: Mapped[str] = mapped_column(String, primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float] = mapped_column(Float(53), nullable=False, default=1.0)

    __table_args__ = (Index("ix_concept_name", "scope", "canonical_name"),)


class ConceptSourceRow(Base):
    __tablename__ = "concept_extracted_from"

    scope: Mapped[str] = mapped_column(String, primary_key=True)
    concept_id: Mapped[str] = mapped_column(String, primary_key=True)
    fragment_id: Mapped[str] = mapped_column(String, primary_key=True)


class ConceptEdgeRow(Base):
    __tablename__ = "concept_edge"

    scope: Mapped[str] = mapped_column(String, primary_key=True)
    from_concept: Mapped[str] = mapped_column(String, primary_key=True)
    to_concept: Mapped[str] = mapped_column(String, primary_key=True)
    edge_type: Mapped[str] = mapped_column(String, primary_key=True)
    weight: Mapped[float] = mapped_column(Float(53), nullable=False, default=1.0)

    __table_args__ = (
        Index("ix_edge_from", "scope", "from_concept", "edge_type"),
        Index("ix_edge_to", "scope", "to_concept", "edge_type"),
    )


class ConceptEdgeEvidenceRow(Base):
    __tablename__ = "concept_edge_evidence"

    scope: Mapped[str] = mapped_column(String, primary_key=True)
    from_concept: Mapped[str] = mapped_column(String, primary_key=True)
    to_concept: Mapped[str] = mapped_column(String, primary_key=True)
    edge_type: Mapped[str] = mapped_column(String, primary_key=True)
    fragment_id: Mapped[str] = mapped_column(String, primary_key=True)
