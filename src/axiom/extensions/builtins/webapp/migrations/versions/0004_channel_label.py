# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A readable name laid over the one a channel was acquired under.

`NCDT1:HEAT:TC-CP1_1` is what somebody else's instrument calls a thing. It is
the channel's identity and it is never rewritten, because a rename forks the
data. This table is a serving-time overlay and nothing else reads it.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

SCHEMA = "webapp"


def upgrade() -> None:
    op.create_table(
        "channel_label",
        sa.Column("site", sa.String(64), primary_key=True),
        sa.Column("feed", sa.String(128), primary_key=True),
        sa.Column("channel", sa.String(256), primary_key=True),
        sa.Column("label", sa.String(128), nullable=False),
        # Who named it, so a name that turns out to be wrong has somebody to
        # ask. Keyed by SITE rather than by person: naming a channel is naming
        # the thing, not one reader's view of it.
        sa.Column("set_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("channel_label", schema=SCHEMA)
