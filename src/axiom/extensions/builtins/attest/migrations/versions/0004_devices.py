# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Enrolled signing devices (ADR-146).

``attest_devices`` is administration state, not evidence: a device is
enrolled and retired, and a retired row keeps who retired it and when. The
facts a signature relied on are copied into the grant and the record.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    tz = sa.DateTime(timezone=True)
    op.create_table(
        "attest_devices",
        sa.Column("device_id", sa.String(128), primary_key=True),
        sa.Column("site_id", sa.String(128), nullable=False, index=True),
        sa.Column("device_class", sa.String(16), nullable=False),
        sa.Column("location", sa.String(64), nullable=True),
        sa.Column("mobility", sa.String(16), nullable=False),
        sa.Column("enrolled_by", sa.String(256), nullable=False),
        sa.Column("enrolled_at", tz, nullable=False),
        sa.Column("retired_by", sa.String(256), nullable=True),
        sa.Column("retired_at", tz, nullable=True),
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_table("attest_devices", schema=schema)
