# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Sealed periods archived as verifiable packages (ADR-251 D7, unit LD-5).

``attest_archives`` is working state, one row per sealed period packaged: where the package is
(through the storage-provider abstraction), its package digest, the stored verify report for
exactly that digest, and whether reads of the period come from it. The package is the evidence;
this row only finds it. No signed row is read differently and no digest changes.

Expand only (ADR-182): one new table.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    op.create_table(
        "attest_archives",
        sa.Column("archive_id", sa.String(36), primary_key=True),
        sa.Column("site_id", sa.String(128), nullable=False),
        sa.Column("logbook", sa.String(64), nullable=False),
        sa.Column("seal_id", sa.String(36), nullable=False),
        sa.Column("from_seq", sa.BigInteger(), nullable=False),
        sa.Column("to_seq", sa.BigInteger(), nullable=False),
        sa.Column("last_seq", sa.BigInteger(), nullable=False),
        sa.Column("package_digest", sa.String(64), nullable=False),
        sa.Column("package_sha256", sa.String(64), nullable=False),
        sa.Column("storage_id", sa.Text(), nullable=False),
        sa.Column("storage_class", sa.String(32), nullable=False),
        sa.Column("manifest", JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("verify_report", JSONB(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("switch_id", sa.String(64), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_by", sa.String(256), nullable=True),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("site_id", "logbook", "seal_id", name="uq_attest_archives_seal"),
        schema=schema,
    )
    op.create_index("ix_attest_archives_range", "attest_archives",
                    ["site_id", "logbook", "status", "from_seq"], schema=schema)  # fmt: skip


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_table("attest_archives", schema=schema)
