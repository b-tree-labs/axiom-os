# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tiered gold: a log's year moves to a cold table, still read through the chain (unit LD-7).

``attest_tier_moves`` is working state, one row per move of one log's year (ADR-251 D7): the cold
table it built, the range and the digests it proved, and the switch that carried it out
(ADR-197). The cold table itself is created by the move, at run time, as a child of
``attest_records`` (``LIKE ... INCLUDING ALL``, then ``INHERIT``), so every read of the chain and
of the gold view over it sees its rows with no change to any reader.

The append-only guard learns three things, checked in the database for every client:

- **No change during a move.** A row in a range whose move is preparing, prepared, committing or
  rolling back is neither updated nor deleted (not even by a committed tombstone's erasure).
- **A row leaves a tier only for an identical copy.** A hot row may be deleted only while its
  move is ``committing`` and an identical row (every column) sits in the cold table that move
  names; a cold row only while its move is ``rolling_back`` and an identical row is back in the
  hot table. Any other DELETE, and every TRUNCATE, is still refused: there is no drop in place
  of a move.
- **One copy of a record across tiers.** An insert into the hot table is refused when a cold
  table already holds a record with its id, digest or position, except the rollback's own copy
  back. A cold table accepts rows only from its own move while it prepares, each identical to a
  hot row.

Expand only (ADR-182): one new table, two new functions and one new trigger; the records guard is
replaced, never dropped.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

#: The statuses in which a move holds its range still.
MOVING = "('preparing', 'prepared', 'committing', 'rolling_back')"


def _refuse() -> str:
    return ("RAISE EXCEPTION 'attest: % is append-only; % refused', TG_TABLE_NAME, TG_OP "
            "USING ERRCODE = 'insufficient_privilege';")  # fmt: skip


def _same(schema: str, row: str, home: str) -> str:
    """SQL true when a row identical to ``row`` (every column but the generated search vector) is
    held by the table whose oid is ``home``."""
    return f"""EXISTS (
        SELECT 1 FROM "{schema}".attest_records twin
        WHERE twin.attestation_id = {row}.attestation_id AND twin.tableoid = {home}
          AND (to_jsonb(twin) - 'search_vector') = (to_jsonb({row}) - 'search_vector'))"""


def _tombstone_body(schema: str) -> str:
    """The erasure 0019 allows (a committed tombstone later in the chain names the row)."""
    return f"""
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


def _records_guard(schema: str, *, with_moves: bool) -> str:
    """The body of ``attest_records_guard()``, with or without the move rules."""
    moves = ""
    if with_moves:
        hot = f"""'"{schema}".attest_records'::regclass"""
        cold = "to_regclass(quote_ident(TG_TABLE_SCHEMA) || '.' || quote_ident(m.table_name))"
        moves = f"""
        SELECT m.* INTO mv FROM "{schema}".attest_tier_moves m
         WHERE m.site_id = OLD.site_id AND m.logbook = OLD.logbook
           AND OLD.seq BETWEEN m.from_seq AND m.to_seq AND m.status IN {MOVING}
         LIMIT 1;
        IF FOUND THEN
            IF TG_OP = 'DELETE' AND mv.status = 'committing' AND TG_RELID = {hot}
               AND {_same(schema, "OLD", cold.replace("m.table_name", "mv.table_name"))} THEN
                RETURN OLD;
            END IF;
            IF TG_OP = 'DELETE' AND mv.status = 'rolling_back' AND TG_TABLE_NAME = mv.table_name
               AND {_same(schema, "OLD", hot)} THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'attest: % #% is being moved between storage tiers; % refused',
                OLD.logbook, OLD.seq, TG_OP USING ERRCODE = 'insufficient_privilege';
        END IF;"""
    declare = "DECLARE mv record;" if with_moves else ""
    return f"""
        {declare}
        BEGIN
            {moves}
            {_tombstone_body(schema)}
            {_refuse()}
        END"""


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    op.create_table(
        "attest_tier_moves",
        sa.Column("move_id", sa.String(36), primary_key=True),
        sa.Column("site_id", sa.String(128), nullable=False),
        sa.Column("logbook", sa.String(64), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("from_seq", sa.BigInteger(), nullable=False),
        sa.Column("to_seq", sa.BigInteger(), nullable=False),
        sa.Column("rows", sa.BigInteger(), nullable=False),
        sa.Column("table_name", sa.String(63), nullable=False),
        sa.Column("tablespace", sa.String(63), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("gold_digest", sa.String(64), nullable=True),
        sa.Column("digests_sha256", sa.String(64), nullable=True),
        sa.Column("archives", JSONB(), nullable=True),
        sa.Column("switch_id", sa.String(64), nullable=True),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        schema=schema,
    )
    op.create_index("ix_attest_tier_moves_range", "attest_tier_moves",
                    ["site_id", "logbook", "status", "from_seq"], schema=schema)  # fmt: skip
    # One live move per log and year; a rolled-back one may be tried again.
    op.create_index("uq_attest_tier_moves_year", "attest_tier_moves", ["site_id", "logbook", "year"],
                    unique=True, schema=schema, postgresql_where=sa.text("status <> 'rolled_back'"))  # fmt: skip

    op.execute(f"""
        CREATE OR REPLACE FUNCTION "{schema}".attest_records_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$ {_records_guard(schema, with_moves=True)} $$""")

    hot = f"""'"{schema}".attest_records'::regclass"""
    op.execute(f"""
        CREATE OR REPLACE FUNCTION "{schema}".attest_records_insert_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE c record;
        BEGIN
            FOR c IN SELECT r.tableoid AS home, r.attestation_id
                       FROM "{schema}".attest_records r
                      WHERE r.tableoid <> TG_RELID
                        AND (r.attestation_id = NEW.attestation_id OR r.digest = NEW.digest
                             OR (r.site_id = NEW.site_id AND r.logbook = NEW.logbook AND r.seq = NEW.seq))
            LOOP
                IF NOT EXISTS (
                    SELECT 1 FROM "{schema}".attest_tier_moves m
                     WHERE m.status = 'rolling_back' AND m.site_id = NEW.site_id AND m.logbook = NEW.logbook
                       AND NEW.seq BETWEEN m.from_seq AND m.to_seq
                       AND to_regclass(quote_ident(TG_TABLE_SCHEMA) || '.' || quote_ident(m.table_name)) = c.home)
                   OR NOT {_same(schema, "NEW", "c.home")} THEN
                    RAISE EXCEPTION 'attest: record %/#% is already held in another storage tier; INSERT refused',
                        NEW.logbook, NEW.seq USING ERRCODE = 'insufficient_privilege';
                END IF;
            END LOOP;
            RETURN NEW;
        END
        $$""")  # fmt: skip
    op.execute(f'DROP TRIGGER IF EXISTS attest_records_one_copy ON "{schema}".attest_records')
    op.execute(f'CREATE TRIGGER attest_records_one_copy BEFORE INSERT ON "{schema}".attest_records '
               f'FOR EACH ROW EXECUTE FUNCTION "{schema}".attest_records_insert_guard()')  # fmt: skip

    # A cold table's own insert guard (attached by the move to each table it creates).
    op.execute(f"""
        CREATE OR REPLACE FUNCTION "{schema}".attest_cold_copy_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM "{schema}".attest_tier_moves m
                 WHERE m.status = 'preparing' AND m.table_name = TG_TABLE_NAME
                   AND m.site_id = NEW.site_id AND m.logbook = NEW.logbook
                   AND NEW.seq BETWEEN m.from_seq AND m.to_seq)
               AND {_same(schema, "NEW", hot)} THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'attest: % takes rows only from its own move; INSERT refused', TG_TABLE_NAME
                USING ERRCODE = 'insufficient_privilege';
        END
        $$""")  # fmt: skip


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.execute(f'DROP TRIGGER IF EXISTS attest_records_one_copy ON "{schema}".attest_records')
    op.execute(f"""
        CREATE OR REPLACE FUNCTION "{schema}".attest_records_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$ {_records_guard(schema, with_moves=False)} $$""")
    op.execute(f'DROP FUNCTION IF EXISTS "{schema}".attest_records_insert_guard()')
    op.execute(f'DROP FUNCTION IF EXISTS "{schema}".attest_cold_copy_guard()')
    op.drop_table("attest_tier_moves", schema=schema)
