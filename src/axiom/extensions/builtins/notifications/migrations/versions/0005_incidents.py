# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Incidents: notifications that name a cause, held as one row per cause
(ADR-249 D2, ADR-252).

Senders run in other processes than the incident stage's tick, so the stage
keeps everything it decides from here, and a restart loses no incident and no
hold window.

Only adds (ADR-182): a new table, nothing existing is altered.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

FALLBACK_SCHEMA = "notifications"  # ADR-052: never the public schema


def _schema() -> str:
    """The schema the migration environment targets, never a literal: a
    hardcoded name creates tables where ``session_for`` does not look."""
    return op.get_context().version_table_schema or FALLBACK_SCHEMA


def upgrade() -> None:
    schema = _schema()
    ts = sa.DateTime(timezone=True)
    op.create_table(
        "incidents",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("fingerprint", sa.String(), nullable=False, unique=True),
        sa.Column("area", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("error_class", sa.String(), nullable=False),
        sa.Column("priority", sa.String(), nullable=False),
        sa.Column("owner_agent", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("remedy", sa.String(), nullable=False),
        sa.Column("unmet", sa.JSON(), nullable=False),
        sa.Column("delivery", sa.JSON(), nullable=False),
        sa.Column("subjects", sa.JSON(), nullable=False),
        sa.Column("first_seen", ts, nullable=False),
        sa.Column("last_seen", ts, nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("hold_until", ts, nullable=True),
        sa.Column("attempts", sa.JSON(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("reopened", sa.Integer(), nullable=False),
        sa.Column("notified_at", ts, nullable=True),
        sa.Column("snoozed_until", ts, nullable=True),
        sa.Column("notes", sa.JSON(), nullable=False),
        sa.Column("closed_at", ts, nullable=True),
        sa.Column("updated_at", ts, nullable=False),
        schema=schema,
    )
    op.create_index("ix_incidents_state", "incidents", ["state"], schema=schema)


def downgrade() -> None:
    schema = _schema()
    op.drop_index("ix_incidents_state", table_name="incidents", schema=schema)
    op.drop_table("incidents", schema=schema)
