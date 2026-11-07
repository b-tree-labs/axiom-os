# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One-time device claim codes (ADR-146).

An enrolled device's browser proves it is that device by redeeming a code
issued at enrolment. Only the code's SHA-256 is stored, with its expiry; both
are cleared when it is spent.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    op.add_column(
        "attest_devices", sa.Column("claim_hash", sa.String(64), nullable=True), schema=schema
    )
    op.add_column(
        "attest_devices",
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_column("attest_devices", "claim_expires_at", schema=schema)
    op.drop_column("attest_devices", "claim_hash", schema=schema)
