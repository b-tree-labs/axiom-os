# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Cross-check facts and predicate events (#1816, spec "Post-sign cross-check hook").

Both are system facts beside the chain, never in it: a cross-check names the signed record it
compared (``attestation_id``) and never changes it; a predicate event records each change of a
predicate's state at a site. Append-only like the other evidence, and each row carries the
labels of the log it belongs to (unit LD-1).

Expand only (ADR-182): two new tables. No row of an existing table is written.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def _append_only(schema: str, table: str) -> None:
    for trigger, timing in (
        ("append_only", "BEFORE UPDATE OR DELETE ON {t} FOR EACH ROW"),
        ("no_truncate", "BEFORE TRUNCATE ON {t} FOR EACH STATEMENT"),
    ):
        op.execute(
            f"CREATE TRIGGER {table}_{trigger} "
            + timing.format(t=f'"{schema}".{table}')
            + f' EXECUTE FUNCTION "{schema}".attest_refuse_change()'
        )


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    tz = sa.DateTime(timezone=True)
    op.create_table(
        "attest_crosschecks",
        sa.Column("fact_id", sa.String(36), primary_key=True),
        sa.Column("site_id", sa.String(128), nullable=False),
        sa.Column("logbook", sa.String(64), nullable=False),
        sa.Column("attestation_id", sa.String(36), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("field", sa.String(64), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("tolerance", sa.String(64), nullable=True),
        sa.Column("source_value", JSONB(), nullable=True),
        sa.Column("detail", JSONB(), nullable=True),
        sa.Column("labels", JSONB(), nullable=False),
        sa.Column("occurred_at", tz, nullable=False),
        sa.Column("checked_at", tz, nullable=False),
        sa.UniqueConstraint("attestation_id", "field", name="attest_crosschecks_once"),
        schema=schema,
    )
    op.create_index("ix_attest_crosschecks_log", "attest_crosschecks",
                    ["site_id", "logbook", "seq"], schema=schema)  # fmt: skip
    _append_only(schema, "attest_crosschecks")
    op.create_table(
        "attest_predicate_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("site_id", sa.String(128), nullable=False),
        sa.Column("logbook", sa.String(64), nullable=False),
        sa.Column("predicate", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("source_value", JSONB(), nullable=True),
        sa.Column("labels", JSONB(), nullable=False),
        sa.Column("as_of", tz, nullable=True),
        sa.Column("at", tz, nullable=False),
        schema=schema,
    )
    op.create_index("ix_attest_predicate_events_log", "attest_predicate_events",
                    ["site_id", "logbook", "predicate", "at"], schema=schema)  # fmt: skip
    _append_only(schema, "attest_predicate_events")


def downgrade() -> None:
    raise RuntimeError("attest 0021 cannot be downgraded: it would delete recorded facts.")
