# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Initial schema for the vault extension.

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
        "capabilities",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("issuer", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False, index=True),
        sa.Column("intent_pattern", sa.String(), nullable=False),
        sa.Column("resource_pattern", sa.String(), nullable=False),
        sa.Column("classification_ceiling", sa.String(), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("delegation_depth", sa.Integer(), nullable=False),
        sa.Column("parent_capability", sa.String(), nullable=True),
        sa.Column("secret_ref", sa.String(), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        schema="vault",
    )
    op.create_table(
        "revocations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("capability_id", sa.String(), nullable=False, index=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        schema="vault",
    )
    op.create_table(
        "secret_refs",
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("classification", sa.String(), nullable=False),
        sa.Column("backend", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="vault",
    )
    op.create_table(
        "outbound_receipts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("called_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("capability_id", sa.String(), nullable=False, index=True),
        sa.Column("actor", sa.String(), nullable=False, index=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        schema="vault",
    )


def downgrade() -> None:
    op.drop_table("outbound_receipts", schema="vault")
    op.drop_table("secret_refs", schema="vault")
    op.drop_table("revocations", schema="vault")
    op.drop_table("capabilities", schema="vault")
