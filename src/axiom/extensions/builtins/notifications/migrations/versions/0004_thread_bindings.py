# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Reply bindings on ``threads``: who a reply binds back to, by which token, and
whether it has already acted.

``DbThreadStore`` keeps reply bindings here instead of in process memory.
``actor`` and ``token`` are what the in-memory store held; ``resolved_at``
makes a reply act once, and ``claim_id`` names the claim allowed to reopen it.
``receipt_id`` becomes nullable because an in-thread
answer mints a correlation with no delivery receipt behind it.

Only adds and relaxes (ADR-182). The table had no reader or writer before
this, so there is nothing to backfill.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

FALLBACK_SCHEMA = "notifications"  # ADR-052: never the public schema


def _schema() -> str:
    """The schema the migration environment targets, never a literal: a
    hardcoded name creates columns where ``session_for`` does not look."""
    return op.get_context().version_table_schema or FALLBACK_SCHEMA


def upgrade() -> None:
    schema = _schema()
    op.add_column("threads", sa.Column("actor", sa.String(), nullable=True), schema=schema)
    op.add_column("threads", sa.Column("token", sa.String(), nullable=True), schema=schema)
    op.add_column(
        "threads",
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        schema=schema,
    )
    op.add_column("threads", sa.Column("claim_id", sa.String(), nullable=True), schema=schema)
    op.create_index("ix_threads_token", "threads", ["token"], schema=schema)
    op.alter_column("threads", "receipt_id", existing_type=sa.String(), nullable=True, schema=schema)


def downgrade() -> None:
    schema = _schema()
    op.alter_column("threads", "receipt_id", existing_type=sa.String(), nullable=False, schema=schema)
    op.drop_index("ix_threads_token", table_name="threads", schema=schema)
    op.drop_column("threads", "claim_id", schema=schema)
    op.drop_column("threads", "resolved_at", schema=schema)
    op.drop_column("threads", "token", schema=schema)
    op.drop_column("threads", "actor", schema=schema)
