# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What makes a graduation count trustworthy.

``graduation_resolutions`` has one row per resolved receipt, so the database
refuses a second resolution of the same receipt. It is a new table rather than an
index on ``verdicts`` because every decision writes to ``verdicts``.

``uq_graduation_actor_intent_resource`` makes the key prd-axiom-authz §5.3 already
states real, so two first approvals racing share one row. ``graduation`` has no
writer before this, so it holds nothing that could collide, and building the
index holds up no decision: the only reader there takes no lock it conflicts with.

Only adds (ADR-182).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "graduation_resolutions",
        sa.Column("resolved_receipt_id", sa.String(), primary_key=True),
        sa.Column("resolution_id", sa.String(), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        schema="authz",
    )
    op.create_index(
        "uq_graduation_actor_intent_resource",
        "graduation",
        ["actor", "intent_class", "resource_pattern"],
        unique=True,
        schema="authz",
    )


def downgrade() -> None:
    op.drop_index(
        "uq_graduation_actor_intent_resource", table_name="graduation", schema="authz"
    )
    op.drop_table("graduation_resolutions", schema="authz")
