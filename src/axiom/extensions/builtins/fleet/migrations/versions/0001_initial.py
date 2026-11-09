# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Initial schema for the fleet extension (spec-fleet-console §2).

Four tables:

- ``fleet_nodes`` — one row per enrolled node (site from credential)
- ``fleet_reports`` — append-only pushed reports
- ``fleet_latest`` — latest-per-(node, kind) projection
- ``fleet_pins`` — declared release state (drift view, P2)

Every op names ``schema=SCHEMA`` explicitly: ``engine_for`` does not set
``search_path``, so an unqualified op writes to ``public`` silently (the
class ``test_migrations_stay_in_their_schema`` guards).

Revision ID: 0001
Revises:
Create Date: 2026-09-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "fleet"


def upgrade() -> None:
    op.create_table(
        "fleet_nodes",
        sa.Column("node_id", sa.String(128), primary_key=True),
        sa.Column("site", sa.String(128), nullable=False, index=True),
        sa.Column("display_name", sa.String(256), nullable=True),
        sa.Column("profile", sa.String(64), nullable=True),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cadences", sa.JSON(), nullable=True),
        schema=SCHEMA,
    )
    op.create_table(
        "fleet_reports",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "node_id",
            sa.String(128),
            sa.ForeignKey(f"{SCHEMA}.fleet_nodes.node_id"),
            nullable=False,
            index=True,
        ),
        sa.Column("site", sa.String(128), nullable=False, index=True),
        sa.Column("kind", sa.String(64), nullable=False, index=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("reporter_principal", sa.String(256), nullable=False),
        sa.Column("envelope_hash", sa.String(128), nullable=True),
        sa.Column(
            "signature_state",
            sa.String(16),
            nullable=False,
            server_default="unverified",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "fleet_latest",
        sa.Column("node_id", sa.String(128), primary_key=True),
        sa.Column("kind", sa.String(64), primary_key=True),
        sa.Column(
            "report_id",
            sa.String(64),
            sa.ForeignKey(f"{SCHEMA}.fleet_reports.id"),
            nullable=False,
        ),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "fleet_pins",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("site", sa.String(128), nullable=False, index=True),
        sa.Column("scope", sa.String(128), nullable=False),
        sa.Column("channel", sa.String(64), nullable=False, server_default="stable"),
        sa.Column("declared_version", sa.String(64), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("declared_by", sa.String(256), nullable=False),
        sa.Column("declared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("fleet_pins", schema=SCHEMA)
    op.drop_table("fleet_latest", schema=SCHEMA)
    op.drop_table("fleet_reports", schema=SCHEMA)
    op.drop_table("fleet_nodes", schema=SCHEMA)
