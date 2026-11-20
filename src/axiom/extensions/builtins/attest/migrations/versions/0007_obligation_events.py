# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Obligation events: warned, missed and met (spec Obligations).

A miss is a fact, so the table is append-only like the other evidence. One
row per (site, logbook, obligation, interval number, due time, state): the tick
records each change once however often it runs.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    tz = sa.DateTime(timezone=True)
    op.create_table(
        "attest_obligation_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("site_id", sa.String(128), nullable=False),
        sa.Column("logbook", sa.String(64), nullable=False),
        sa.Column("obligation", sa.String(64), nullable=False),
        sa.Column("interval_kind", sa.String(64), nullable=False),
        sa.Column("interval_number", sa.BigInteger(), nullable=False),
        sa.Column("due_at", tz, nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("at", tz, nullable=False),
        sa.UniqueConstraint(
            "site_id",
            "logbook",
            "obligation",
            "interval_number",
            "due_at",
            "state",
            name="attest_obligation_events_once",
        ),
        schema=schema,
    )
    for trigger, timing in (
        ("append_only", "BEFORE UPDATE OR DELETE ON {t} FOR EACH ROW"),
        ("no_truncate", "BEFORE TRUNCATE ON {t} FOR EACH STATEMENT"),
    ):
        op.execute(
            f"CREATE TRIGGER attest_obligation_events_{trigger} "
            + timing.format(t=f'"{schema}".attest_obligation_events')
            + f' EXECUTE FUNCTION "{schema}".attest_refuse_change()'
        )


def downgrade() -> None:
    raise RuntimeError("attest 0007 cannot be downgraded: it would delete recorded misses.")
