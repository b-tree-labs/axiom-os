# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Initial notifications schema.

Revision ID: 0001
Revises:
Create Date: 2026-05-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "notifications"


def upgrade() -> None:
    op.create_table(
        "delivery_receipts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("envelope_json", sa.JSON(), nullable=False),
        sa.Column("intent", sa.String(), nullable=False, index=True),
        sa.Column("actor", sa.String(), nullable=False, index=True),
        sa.Column("recipient", sa.String(), nullable=False, index=True),
        sa.Column("classification", sa.String(), nullable=False),
        sa.Column("priority", sa.String(), nullable=False),
        sa.Column("channel_selected", sa.String(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("vendor_correlation", sa.String(), nullable=True),
        sa.Column("correlation_id", sa.String(), nullable=False, index=True),
        sa.Column("routing_rationale", sa.JSON(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("fragment_ref", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "notifications_inbox",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("receipt_id", sa.String(),
                  sa.ForeignKey(f"{SCHEMA}.delivery_receipts.id"),
                  nullable=False, index=True),
        sa.Column("recipient", sa.String(), nullable=False, index=True),
        sa.Column("classification", sa.String(), nullable=False),
        sa.Column("priority", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("body_ref", sa.String(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("muted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "threads",
        sa.Column("correlation_id", sa.String(), primary_key=True),
        sa.Column("receipt_id", sa.String(),
                  sa.ForeignKey(f"{SCHEMA}.delivery_receipts.id"),
                  nullable=False, index=True),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("vendor_thread_id", sa.String(), nullable=True),
        sa.Column("cross_channel", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "channel_preferences",
        sa.Column("recipient", sa.String(), primary_key=True),
        sa.Column("classification", sa.String(), primary_key=True),
        sa.Column("priority", sa.String(), primary_key=True),
        sa.Column("ordered_channels", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "channel_registry",
        sa.Column("recipient", sa.String(), primary_key=True),
        sa.Column("channel", sa.String(), primary_key=True),
        sa.Column("address_ref", sa.String(), nullable=False),
        sa.Column("classification", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "dedup_log",
        sa.Column("primitive", sa.String(), primary_key=True),
        sa.Column("actor", sa.String(), primary_key=True),
        sa.Column("dedup_key", sa.String(), primary_key=True),
        sa.Column("receipt_id", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True),
                  nullable=False, index=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("dedup_log", schema=SCHEMA)
    op.drop_table("channel_registry", schema=SCHEMA)
    op.drop_table("channel_preferences", schema=SCHEMA)
    op.drop_table("threads", schema=SCHEMA)
    op.drop_table("notifications_inbox", schema=SCHEMA)
    op.drop_table("delivery_receipts", schema=SCHEMA)
