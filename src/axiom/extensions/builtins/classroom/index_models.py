# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SQLAlchemy models for the ``classroom`` Postgres schema: the per-classroom search index (ADR-174).

A *scope* names one classroom's index (it replaces the one-file-per-classroom layout), and every
row carries it, so classrooms never see each other's chunks. Search is Postgres full-text; the
chunk table has a GIN index on its text vector.
"""

from __future__ import annotations

from sqlalchemy import JSON, BigInteger, Float, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EXTENSION_SCHEMA = "classroom"

_PK = BigInteger().with_variant(Integer, "sqlite")
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class IndexChunk(Base):
    __tablename__ = "index_chunk"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    file_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_index_chunk_scope_file", "scope", "file_id"),)


class IndexEntity(Base):
    __tablename__ = "index_entity"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    file_id: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    properties: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    confidence: Mapped[float] = mapped_column(Float(53), nullable=False, default=1.0)

    __table_args__ = (
        Index("ix_index_entity_scope_file", "scope", "file_id"),
        Index("ix_index_entity_name", "scope", "name"),
    )


class IndexEdge(Base):
    __tablename__ = "index_edge"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    file_id: Mapped[str] = mapped_column(String, nullable=False)
    rel_type: Mapped[str] = mapped_column(String, nullable=False)
    from_name: Mapped[str] = mapped_column(Text, nullable=False)
    from_label: Mapped[str] = mapped_column(String, nullable=False)
    to_name: Mapped[str] = mapped_column(Text, nullable=False)
    to_label: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float] = mapped_column(Float(53), nullable=False, default=1.0)

    __table_args__ = (Index("ix_index_edge_scope_file", "scope", "file_id"),)
