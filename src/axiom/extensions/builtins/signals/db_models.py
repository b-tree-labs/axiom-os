# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SQLAlchemy models for Neut Sense database.

Uses PostgreSQL + pgvector for vector similarity search.
Alembic manages migrations based on these model definitions.

Usage:
    from axiom.extensions.builtins.signals.db_models import Signal, Media, Participant, Person
    from axiom.extensions.builtins.signals.db_models import get_engine, get_session

    engine = get_engine()
    with get_session() as session:
        signal = Signal(id="...", text="...", embedding=[...])
        session.add(signal)
        session.commit()
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

# Embedding dimension (OpenAI text-embedding-3-small)
EMBEDDING_DIM = 1536

# Default database URL
DEFAULT_DB_URL = "postgresql://axiom:axiom@localhost:5432/axiom_db"


def get_db_url() -> str:
    """Get database URL from environment or default."""
    return os.environ.get("AXIOM_DB_URL", DEFAULT_DB_URL)


def get_engine(url: str | None = None, **kwargs):
    """Create SQLAlchemy engine.

    Args:
        url: Database URL (defaults to AXIOM_DB_URL or local K3D)
        **kwargs: Additional engine arguments

    Returns:
        SQLAlchemy Engine
    """
    db_url = url or get_db_url()
    return create_engine(
        db_url,
        pool_pre_ping=True,  # Verify connections before use
        **kwargs
    )


def get_session(engine=None) -> Session:
    """Create a database session.

    Args:
        engine: SQLAlchemy engine (creates new if not provided)

    Returns:
        SQLAlchemy Session
    """
    if engine is None:
        engine = get_engine()
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal()


class Base(DeclarativeBase):
    """Base class for all models."""
    pass


def utcnow() -> datetime:
    """Get current UTC timestamp."""
    return datetime.now(UTC)


class Signal(Base):
    """Signal chunk with embedding for RAG.

    Represents a piece of ingested content (voice memo, meeting note, etc.)
    chunked and embedded for vector similarity search.
    """

    __tablename__ = "signals"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list | None] = mapped_column(Vector(EMBEDDING_DIM))

    signal_type: Mapped[str | None] = mapped_column(String(100))
    initiative: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str | None] = mapped_column(String(500))
    timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, default=dict)

    # Audit fields
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    owner_id: Mapped[str | None] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (
        Index("idx_signals_type", "signal_type"),
        Index("idx_signals_initiative", "initiative"),
        Index("idx_signals_timestamp", "timestamp"),
        Index(
            "signals_embedding_idx",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    def __repr__(self) -> str:
        return f"<Signal(id={self.id!r}, type={self.signal_type!r})>"


class Media(Base):
    """Media recording with transcript and embedding.

    Represents audio/video recordings that have been transcribed
    and embedded for search.
    """

    __tablename__ = "media"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(50))  # audio, video
    title: Mapped[str | None] = mapped_column(String(500))

    transcript: Mapped[str | None] = mapped_column(Text)
    transcript_preview: Mapped[str | None] = mapped_column(String(1000))
    embedding: Mapped[list | None] = mapped_column(Vector(EMBEDDING_DIM))

    duration_sec: Mapped[float | None] = mapped_column(Float)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Access control
    owner_id: Mapped[str | None] = mapped_column(String(255))

    # Audit fields
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    version: Mapped[int] = mapped_column(Integer, default=1)

    # Relationships
    participants: Mapped[list[Participant]] = relationship(
        back_populates="media",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_media_owner", "owner_id"),
        Index("idx_media_recorded", "recorded_at"),
        Index(
            "media_embedding_idx",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    def __repr__(self) -> str:
        return f"<Media(id={self.id!r}, title={self.title!r})>"


class Participant(Base):
    """Participant in a media recording.

    Links people to media with role and access control information.
    """

    __tablename__ = "participants"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    media_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("media.id", ondelete="CASCADE"),
        nullable=False,
    )
    person_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))

    role: Mapped[str | None] = mapped_column(String(100))  # speaker, mentioned, attendee
    access_level: Mapped[str] = mapped_column(
        String(50), default="participant"
    )  # owner, participant, shared, none
    mention_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    # Relationships
    media: Mapped[Media] = relationship(back_populates="participants")

    __table_args__ = (
        Index("idx_participants_media", "media_id"),
        Index("idx_participants_person", "person_id"),
    )

    def __repr__(self) -> str:
        return f"<Participant(id={self.id!r}, name={self.name!r})>"


class Person(Base):
    """Person registry.

    Central registry of people with aliases for name resolution
    across different sources.
    """

    __tablename__ = "people"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    aliases: Mapped[list | None] = mapped_column(ARRAY(String(255)))
    email: Mapped[str | None] = mapped_column(String(500))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    def __repr__(self) -> str:
        return f"<Person(id={self.id!r}, name={self.name!r})>"


# All models for Alembic autogenerate
ALL_MODELS = [Signal, Media, Participant, Person]
