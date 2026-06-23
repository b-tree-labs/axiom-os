# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Initial schema for the authz extension — verdicts, policies, graduation.

Revision ID: 0001
Revises:
Create Date: 2026-05-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "verdicts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("actor", sa.String(), nullable=False, index=True),
        sa.Column("intent", sa.String(), nullable=False, index=True),
        sa.Column("resource", sa.String(), nullable=False, index=True),
        sa.Column("classification", sa.String(), nullable=False),
        sa.Column("capability_id", sa.String(), nullable=False),
        sa.Column("context_fragment_id", sa.String(), nullable=False),
        sa.Column("provenance_parent", sa.String(), nullable=False),
        sa.Column("federation_origin", sa.String(), nullable=True, index=True),
        sa.Column("dedup_key", sa.String(), nullable=False, index=True),
        sa.Column("decision", sa.String(), nullable=False, index=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("matched_rules", sa.JSON(), nullable=True),
        schema="authz",
    )
    op.create_table(
        "policies",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), unique=True, nullable=False),
        sa.Column("intent_pattern", sa.String(), nullable=False),
        sa.Column("actor_pattern", sa.String(), nullable=False),
        sa.Column("resource_pattern", sa.String(), nullable=False),
        sa.Column("classification", sa.String(), nullable=False),
        sa.Column("federation_origin_pattern", sa.String(), nullable=True),
        sa.Column("disposition", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ttl", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="authz",
    )
    op.create_table(
        "graduation",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("actor", sa.String(), nullable=False, index=True),
        sa.Column("intent_class", sa.String(), nullable=False, index=True),
        sa.Column("resource_pattern", sa.String(), nullable=False),
        sa.Column("approvals", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("threshold", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("graduated", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("last_update", sa.DateTime(timezone=True), nullable=False),
        schema="authz",
    )


def downgrade() -> None:
    op.drop_table("graduation", schema="authz")
    op.drop_table("policies", schema="authz")
    op.drop_table("verdicts", schema="authz")
