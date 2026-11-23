# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A journal row loses the words of a record whose content was removed (unit LD-6).

The journal is an index of references (ADR-163 as amended). A reference to a signed record
carries the record's title as its summary, and the attestations lane is kept for life, so its
partition is never dropped: when a record's content is removed under its retention rule (a
signed tombstone), the summary is the one copy of its words left here.

``journal_erasures`` is append only: one row per reference whose words were removed, naming the
source, the source's id, the summary that replaces the words (the type's label) and why (the
tombstone). The append-only guard on ``journal_events`` gains exactly one allowed change: a row's
``summary`` set to the replacement an erasure row names for its ``(source, source_id)``, every
other column unchanged. Any other UPDATE, every DELETE and every TRUNCATE is still refused;
pruning stays whole partitions.

Expand only (ADR-182): one new table; the guard function is replaced, never dropped.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _schema() -> str:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("journal migrations need version_table_schema (ADR-052)")
    return schema


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "journal_erasures",
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(160), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("reason", JSONB(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("source", "source_id"),
        schema=schema,
    )
    op.execute(f'CREATE TRIGGER journal_erasures_append_only BEFORE UPDATE OR DELETE ON "{schema}".journal_erasures '
               f'FOR EACH ROW EXECUTE FUNCTION "{schema}".journal_refuse_change()')  # fmt: skip
    op.execute(f'CREATE TRIGGER journal_erasures_no_truncate BEFORE TRUNCATE ON "{schema}".journal_erasures '
               f'FOR EACH STATEMENT EXECUTE FUNCTION "{schema}".journal_refuse_change()')  # fmt: skip
    op.execute(f"""
        CREATE OR REPLACE FUNCTION "{schema}".journal_events_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE'
               AND (to_jsonb(NEW) - 'summary') = (to_jsonb(OLD) - 'summary')
               AND EXISTS (SELECT 1 FROM "{schema}".journal_erasures e
                           WHERE e.source = OLD.source AND e.source_id = OLD.source_id
                             AND e.summary = NEW.summary)
            THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'journal: % is append-only; % refused', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END
        $$""")  # fmt: skip
    op.execute(f'DROP TRIGGER IF EXISTS journal_events_append_only ON "{schema}".journal_events')
    op.execute(f'CREATE TRIGGER journal_events_append_only BEFORE UPDATE OR DELETE ON "{schema}".journal_events '
               f'FOR EACH ROW EXECUTE FUNCTION "{schema}".journal_events_guard()')  # fmt: skip


def downgrade() -> None:
    schema = _schema()
    op.execute(f'DROP TRIGGER IF EXISTS journal_events_append_only ON "{schema}".journal_events')
    op.execute(f'CREATE TRIGGER journal_events_append_only BEFORE UPDATE OR DELETE ON "{schema}".journal_events '
               f'FOR EACH ROW EXECUTE FUNCTION "{schema}".journal_refuse_change()')  # fmt: skip
    op.execute(f'DROP FUNCTION IF EXISTS "{schema}".journal_events_guard()')
    op.drop_table("journal_erasures", schema=schema)
