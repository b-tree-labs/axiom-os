# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The decision record: append-only case verdicts (typed decision
receipts, ADR-126) with a later-arriving outcome.

Every op names ``schema=SCHEMA`` explicitly (see 0001).

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA = "receipts"


def upgrade() -> None:
    op.create_table(
        "case_verdict",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("site", sa.String(200), nullable=False, index=True),
        sa.Column("case_id", sa.String(40), nullable=False, index=True),
        sa.Column("decision_type", sa.String(20), nullable=False, server_default="choice"),
        sa.Column("options", sa.JSON, nullable=False),
        sa.Column("chosen", sa.String(40), nullable=False),
        sa.Column("decider", sa.String(200), nullable=False),
        sa.Column("decider_kind", sa.String(20), nullable=False, server_default="human"),
        sa.Column("evidence", sa.Text, nullable=False, server_default=""),
        sa.Column("note", sa.String(2000), nullable=False, server_default=""),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(40), nullable=True),
        sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_case_verdict_site_case",
        "case_verdict",
        ["site", "case_id"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_case_verdict_site_case", "case_verdict", schema=SCHEMA)
    op.drop_table("case_verdict", schema=SCHEMA)
