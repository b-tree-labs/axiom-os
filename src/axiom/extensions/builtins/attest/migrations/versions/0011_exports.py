# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Exports (logbook unit L6): one row per evidence package a person asked for.

``attest_exports`` is working state, not evidence: the package itself is in
``axiom.infra.storage`` and holds the whole chain, so this table carries no
signed content and no append-only trigger. Expand only.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    op.create_table(
        "attest_exports",
        sa.Column("export_id", sa.String(36), primary_key=True),
        sa.Column("site_id", sa.String(128), nullable=False),
        sa.Column("logbook", sa.String(64), nullable=False),
        sa.Column("requested_by", sa.String(256), nullable=False),
        sa.Column("filter", JSONB(), nullable=False),
        sa.Column("formats", JSONB(), nullable=False),
        sa.Column("counts", JSONB(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("storage_id", sa.Text(), nullable=True),
        sa.Column("manifest_sha256", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )
    op.create_index(
        "attest_exports_site_logbook_created",
        "attest_exports",
        ["site_id", "logbook", "created_at"],
        schema=schema,
    )


def downgrade() -> None:
    raise RuntimeError(
        "attest 0011 cannot be downgraded: it would forget which evidence packages were handed out."
    )
