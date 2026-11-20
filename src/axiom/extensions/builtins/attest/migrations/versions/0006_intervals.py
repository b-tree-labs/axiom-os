# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Intervals (runs and the like) opened and closed by signed entries.

``attest_intervals`` is a projection of the chain, written in the same
transaction as the record that opens or closes the interval. The record is the
truth: each one carries its interval in signed content. A partial unique index
allows one open interval per (site, logbook, kind), so two concurrent openers
cannot both succeed.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    tz = sa.DateTime(timezone=True)
    op.create_table(
        "attest_intervals",
        sa.Column("site_id", sa.String(128), primary_key=True),
        sa.Column("logbook", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(64), primary_key=True),
        sa.Column("number", sa.BigInteger(), primary_key=True),
        sa.Column("opened_by", sa.String(36), nullable=False),
        sa.Column("opened_at", tz, nullable=False),
        sa.Column("closed_by", sa.String(36), nullable=True),
        sa.Column("closed_at", tz, nullable=True),
        schema=schema,
    )
    op.execute(
        f'CREATE UNIQUE INDEX attest_intervals_one_open ON "{schema}".attest_intervals '
        "(site_id, logbook, kind) WHERE closed_at IS NULL"
    )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_table("attest_intervals", schema=schema)
