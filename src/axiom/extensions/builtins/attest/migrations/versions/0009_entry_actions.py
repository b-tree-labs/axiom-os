# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Entry actions: a record may point at another (logbook unit L2).

A supplement, and later an acknowledgement, co-signature or seal, is its own signed record
that names the record it acts on. ``attest_records.target`` copies that link out of the signed
body for lookup; ``attest_drafts`` carries the action's ``kind``, ``target`` and ``reason``
until it is signed. Expand only (ADR-182): every column is nullable or has a server default,
so no row of ``attest_records`` is rewritten and the append-only triggers do not fire.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    op.add_column(
        "attest_records", sa.Column("target", sa.String(36), nullable=True), schema=schema
    )
    op.create_index("ix_attest_records_target", "attest_records", ["target"], schema=schema)
    op.add_column(
        "attest_drafts",
        sa.Column("kind", sa.String(32), nullable=False, server_default="statement"),
        schema=schema,
    )
    op.add_column("attest_drafts", sa.Column("target", sa.String(36), nullable=True), schema=schema)
    op.add_column("attest_drafts", sa.Column("reason", sa.String(16), nullable=True), schema=schema)


def downgrade() -> None:
    raise RuntimeError(
        "attest 0009 cannot be downgraded: dropping target would cut every signed supplement "
        "from the record it supplements."
    )
