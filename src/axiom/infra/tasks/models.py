# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SQLAlchemy model for the ``tasks`` Postgres schema (ADR-052, ADR-174).

Timestamps stay ISO-8601 text, as they were in the SQLite store: they sort correctly, round-trip
exactly, and carry the offset. ``node_id`` is new: a pid, a working directory and an output file
are facts about one machine, so a task row says which machine, and a shared database never
presents one node's pid as another's.
"""

from __future__ import annotations

from sqlalchemy import JSON, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EXTENSION_SCHEMA = "tasks"

JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class TaskRow(Base):
    __tablename__ = "task"

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    node_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    command: Mapped[list] = mapped_column(JSONType, nullable=False)
    cwd: Mapped[str] = mapped_column(Text, nullable=False)
    spawner_principal: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    output_path: Mapped[str] = mapped_column(Text, nullable=False)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    ended_at: Mapped[str | None] = mapped_column(String, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index("ix_task_node_created", "node_id", "created_at"),
        Index("ix_task_status", "status"),
        Index("ix_task_principal", "spawner_principal"),
    )
