# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The journal schema and its append-only guard (ADR-163; design note J1 section 1).

Tables:

- ``journal_events``: one reference per thing that happened, partitioned by month on ``at``.
  The writer creates each month's partition before its first insert into it. Append only.
  ``lane`` is text with no constraint: the vocabulary is the registry in ``lanes.py`` (the
  journal's eight plus what extensions declare), so a new lane needs no schema change.
- ``journal_keys``: the ``(source, source_id)`` key, once across every partition. Append only.
- ``journal_cursors``: where each catch-up reader resumes. Working state, mutable.

The guard is a trigger, copied from the attest schema, so it holds for every client, the table
owner included: UPDATE and DELETE fail per row and TRUNCATE fails per statement. A row trigger
on a partitioned table is cloned onto every partition, including ones created later. Pruning is
a later unit and drops whole partitions, which is the one path this guard leaves open on purpose.

Every op names the schema, read from ``version_table_schema``, because ``engine_for`` does not
set ``search_path``.

Revision ID: 0001
Revises:
Create Date: 2026-11-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

STATUSES = ("ok", "warn", "bad", "info")
APPEND_ONLY = ("journal_events", "journal_keys")


def _schema() -> str:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("journal migrations need version_table_schema (ADR-052)")
    return schema


def upgrade() -> None:
    schema = _schema()
    tz = sa.DateTime(timezone=True)
    statuses = ", ".join(f"'{s}'" for s in STATUSES)

    op.create_table(
        "journal_events",
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("at", tz, nullable=False),
        sa.Column("id", UUID(as_uuid=False), nullable=False),
        sa.Column("recorded_at", tz, nullable=False),
        sa.Column("lane", sa.String(16), nullable=False),
        sa.Column("subject", sa.String(256), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(160), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("actor", sa.String(256), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("subjects", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("ref", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("link", sa.Text(), nullable=True),
        sa.Column("site", sa.String(128), nullable=True),
        sa.Column("logbook", sa.String(64), nullable=True),
        sa.Column("classification", sa.String(16), nullable=False, server_default="internal"),
        sa.Column("retention_class", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint("seq", "at"),
        sa.CheckConstraint(f"status IN ({statuses})", name="journal_events_status_known"),
        schema=schema,
        postgresql_partition_by="RANGE (at)",
    )
    op.create_index("ix_journal_events_lane_seq", "journal_events", ["lane", "seq"], schema=schema)
    op.create_index("ix_journal_events_site_seq", "journal_events", ["site", "seq"], schema=schema)
    op.create_index(
        "ix_journal_events_source", "journal_events", ["source", "source_id"], schema=schema
    )

    op.create_table(
        "journal_keys",
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(160), nullable=False),
        sa.Column("at", tz, nullable=False),
        sa.PrimaryKeyConstraint("source", "source_id"),
        schema=schema,
    )
    op.create_table(
        "journal_cursors",
        sa.Column("source", sa.String(64), primary_key=True),
        sa.Column("position", JSONB(), nullable=False),
        sa.Column("updated_at", tz, nullable=False),
        schema=schema,
    )

    op.execute(
        f"""
        CREATE FUNCTION "{schema}".journal_refuse_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'journal: % is append-only; % refused', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END
        $$
        """
    )
    for table in APPEND_ONLY:
        op.execute(
            f'CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON "{schema}".{table} '
            f'FOR EACH ROW EXECUTE FUNCTION "{schema}".journal_refuse_change()'
        )
        op.execute(
            f'CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON "{schema}".{table} '
            f'FOR EACH STATEMENT EXECUTE FUNCTION "{schema}".journal_refuse_change()'
        )


def downgrade() -> None:
    # The journal is rebuildable from its sources, but taking a node back past this revision
    # is a deliberate, manual drop, not a routine downgrade: the index is what Audit reads.
    raise RuntimeError(
        "journal 0001 cannot be downgraded: it would delete the node's journal. "
        "Drop the schema by hand if that is intended; catch-up rebuilds it from the lane records."
    )
