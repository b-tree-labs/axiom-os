# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Retention enforced with tombstones (ADR-251 D7, unit LD-6).

``attest_tombstones`` is working state, one row per signed tombstone: the sealed and archived
period it names, the dry run as it was approved, and the switch that carries out the erasure
(ADR-197: ``committed`` hides the content from every read, a named approver's retire removes it).

The append-only guard on ``attest_records``, ``attest_presentations`` and ``attest_responses``
gains exactly one allowed change, checked in the database for every client: removing a covered
record's content. A row may be updated only when

- a signed record of kind ``tombstone`` later in the same chain names that record's sequence and
  digest in its ``erased`` list, and its switch is ``committed`` (approved, not rolled back);
- every other column is unchanged, and the new ``record`` is the old one without ``content`` and
  ``evidence``, plus ``erased = {"tombstone": <its id>}`` (so the digest, the link and the
  signatures stay); a presentation loses only its statement, rendering, forms and speakable text,
  and an answer only its transcript.

Any other UPDATE, every DELETE and every TRUNCATE is still refused. Deletion as an edit or a row
delete stays impossible; the tombstone is a new record, never a change to history.

Expand only (ADR-182): one new table; the guard functions are replaced, never dropped.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

#: The guarded tables, the guard function each gets now, and the columns its erasure may change.
GUARDED = {
    "attest_records": "attest_records_guard",
    "attest_presentations": "attest_presentations_guard",
    "attest_responses": "attest_responses_guard",
}


def _refuse() -> str:
    return ("RAISE EXCEPTION 'attest: % is append-only; % refused', TG_TABLE_NAME, TG_OP "
            "USING ERRCODE = 'insufficient_privilege';")  # fmt: skip


def _covered(schema: str, attestation: str) -> str:
    """SQL true when a committed tombstone in the same chain names ``attestation`` (an SQL
    expression for an attestation id)."""
    return f"""EXISTS (
        SELECT 1 FROM "{schema}".attest_records r
        JOIN "{schema}".attest_records t ON t.site_id = r.site_id AND t.logbook = r.logbook
             AND t.kind = 'tombstone' AND t.seq > r.seq
        JOIN "{schema}".attest_tombstones x ON x.tombstone_id = t.attestation_id AND x.status = 'committed'
        WHERE r.attestation_id = {attestation}
          AND t.record->'content'->'tombstone'->'erased' @> jsonb_build_array(
              jsonb_build_object('attestation_id', r.attestation_id, 'seq', r.seq, 'digest', r.digest)))"""


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    op.create_table(
        "attest_tombstones",
        sa.Column("tombstone_id", sa.String(36), primary_key=True),
        sa.Column("site_id", sa.String(128), nullable=False),
        sa.Column("logbook", sa.String(64), nullable=False),
        sa.Column("seal_id", sa.String(36), nullable=False),
        sa.Column("archive_id", sa.String(36), nullable=False),
        sa.Column("from_seq", sa.BigInteger(), nullable=False),
        sa.Column("to_seq", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("switch_id", sa.String(64), nullable=True),
        sa.Column("plan", JSONB(), nullable=True),
        sa.Column("deletion", JSONB(), nullable=True),
        sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("erased_by", sa.String(256), nullable=True),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("site_id", "logbook", "seal_id", name="uq_attest_tombstones_seal"),
        schema=schema,
    )
    op.create_index("ix_attest_tombstones_range", "attest_tombstones",
                    ["site_id", "logbook", "status", "from_seq"], schema=schema)  # fmt: skip

    records = f"""
        IF TG_OP = 'UPDATE' AND OLD.record ? 'content'
           AND (to_jsonb(NEW) - 'record' - 'search_vector') = (to_jsonb(OLD) - 'record' - 'search_vector')
           AND (NEW.record - 'erased') = (OLD.record - 'content' - 'evidence')
           AND EXISTS (
               SELECT 1 FROM "{schema}".attest_records t
               JOIN "{schema}".attest_tombstones x ON x.tombstone_id = t.attestation_id AND x.status = 'committed'
               WHERE t.site_id = OLD.site_id AND t.logbook = OLD.logbook AND t.kind = 'tombstone'
                 AND t.seq > OLD.seq
                 AND NEW.record->'erased' = jsonb_build_object('tombstone', t.attestation_id)
                 AND t.record->'content'->'tombstone'->'erased' @> jsonb_build_array(
                     jsonb_build_object('attestation_id', OLD.attestation_id, 'seq', OLD.seq,
                                        'digest', OLD.digest)))
        THEN
            RETURN NEW;
        END IF;"""
    presentations = f"""
        IF TG_OP = 'UPDATE' AND NOT (OLD.statement ? 'erased')
           AND (to_jsonb(NEW) - 'statement' - 'rendered' - 'forms' - 'speakable')
               = (to_jsonb(OLD) - 'statement' - 'rendered' - 'forms' - 'speakable')
           AND NEW.rendered = '' AND NEW.forms IS NULL AND NEW.speakable IS NULL
           AND NEW.statement - 'erased' = '{{}}'::jsonb
           AND {_covered(schema, f'(SELECT d.attestation_id FROM "{schema}".attest_drafts d WHERE d.draft_id = OLD.draft_id)')}
        THEN
            RETURN NEW;
        END IF;"""
    responses = f"""
        IF TG_OP = 'UPDATE' AND OLD.transcript IS NOT NULL AND NEW.transcript IS NULL
           AND (to_jsonb(NEW) - 'transcript') = (to_jsonb(OLD) - 'transcript')
           AND {_covered(schema, f'(SELECT d.attestation_id FROM "{schema}".attest_presentations p JOIN "{schema}".attest_drafts d ON d.draft_id = p.draft_id WHERE p.presentation_id = OLD.presentation_id)')}
        THEN
            RETURN NEW;
        END IF;"""
    for table, body in (("attest_records", records), ("attest_presentations", presentations),
                        ("attest_responses", responses)):  # fmt: skip
        fn = GUARDED[table]
        op.execute(f"""
            CREATE OR REPLACE FUNCTION "{schema}".{fn}() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                {body}
                {_refuse()}
            END
            $$""")  # fmt: skip
        op.execute(f'DROP TRIGGER IF EXISTS {table}_append_only ON "{schema}".{table}')
        op.execute(f'CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON "{schema}".{table} '
                   f'FOR EACH ROW EXECUTE FUNCTION "{schema}".{fn}()')  # fmt: skip


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    for table, fn in GUARDED.items():
        op.execute(f'DROP TRIGGER IF EXISTS {table}_append_only ON "{schema}".{table}')
        op.execute(f'CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON "{schema}".{table} '
                   f'FOR EACH ROW EXECUTE FUNCTION "{schema}".attest_refuse_change()')  # fmt: skip
        op.execute(f'DROP FUNCTION IF EXISTS "{schema}".{fn}()')
    op.drop_table("attest_tombstones", schema=schema)
