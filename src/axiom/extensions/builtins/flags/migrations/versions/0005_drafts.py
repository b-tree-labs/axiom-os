# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Drafts: a person's unsigned cohort or rollout, the one object the builder and chat edit.

Expand only (ADR-182): one new table. The release before this one never reads it.

Every op names ``schema=SCHEMA``: ``engine_for`` does not set the search path.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

SCHEMA = "flags"

JSONDoc = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "drafts",
        sa.Column("principal", sa.String(255), primary_key=True),
        sa.Column("site", sa.String(255), primary_key=True),
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("body", JSONDoc, nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(255), nullable=False),
        sa.Column("updated_via", sa.String(16), nullable=False),
        sa.CheckConstraint("kind IN ('cohort', 'rollout')", name="ck_drafts_kind"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("drafts", schema=SCHEMA)
