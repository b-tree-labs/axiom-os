# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Recipient-preferences profile table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-01

ADR-052 §D5 — this table lives in the ``notifications`` schema alongside
the other HERALD tables. We intentionally do NOT add a hard FK from
``recipient_profiles.recipient`` to ``channel_registry.recipient`` or
``delivery_receipts.recipient``: neither is a unique key in its own
table, and any cross-extension reference (recipient identity is
ultimately owned by ``identity``) would ride the data platform per
ADR-052 §D5, not an OLTP join. The recipient string is therefore a
**soft reference** — validated at write time by ``RecipientProfile``'s
``@``-prefix check, but not constrained by the database.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA = "notifications"


def upgrade() -> None:
    op.create_table(
        "recipient_profiles",
        sa.Column("recipient", sa.String(), primary_key=True),
        sa.Column("channels_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("recipient_profiles", schema=SCHEMA)
