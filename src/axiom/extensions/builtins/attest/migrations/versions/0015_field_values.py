# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The field-value index a log's declared filters read (ADR-268 D4).

``attest_field_values`` holds one row per (entry, field, and for ``readings`` the instrument)
and per record the value came from: the entry itself, or a correction that restated the field.
``current`` marks the value a filter compares (the latest signed correction, else the signed
value). Every row carries the labels the door checks (site, log, entry type) and the digest of
the record it came from, the drift anchor (ADR-241 D5). It is a labelled projection of the chain
(ADR-251 D1): derived by sequence number by the projector in ``field_index.py``, typed by the
declaration, rebuildable at any time, and never a source of truth.

``attest_field_watermarks`` holds, per chain, the last sequence number projected and the
projector's version; a new version rebuilds the chain's rows.

Expand only (ADR-182): two new tables and their indexes. No signed row is read differently and
no digest changes.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    op.create_table(
        "attest_field_values",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("site_id", sa.String(128), nullable=False),
        sa.Column("logbook", sa.String(64), nullable=False),
        sa.Column("entry_type", sa.String(64), nullable=True),
        sa.Column("entry_seq", sa.BigInteger(), nullable=False),
        sa.Column("attestation_id", sa.String(36), nullable=False),
        sa.Column("field", sa.String(128), nullable=False),
        sa.Column("instrument", sa.String(128), nullable=False, server_default=""),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_words", sa.dialects.postgresql.TSVECTOR(), nullable=True),
        sa.Column("value_num", sa.Numeric(), nullable=True),
        sa.Column("value_unit", sa.String(64), nullable=True),
        sa.Column("value_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_attestation_id", sa.String(36), nullable=False),
        sa.Column("source_seq", sa.BigInteger(), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("site_id", "logbook", "entry_seq", "field", "instrument", "source_seq",
                            name="uq_attest_field_values_source"),
        schema=schema,
    )
    current = sa.text("current")
    op.create_index(
        "ix_attest_field_values_text", "attest_field_values",
        ["site_id", "logbook", "field", "value_text", "entry_seq"],
        schema=schema, postgresql_where=current,
    )  # fmt: skip
    op.create_index(
        "ix_attest_field_values_num", "attest_field_values",
        ["site_id", "logbook", "field", "value_unit", "value_num", "entry_seq"],
        schema=schema, postgresql_where=current,
    )  # fmt: skip
    op.create_index(
        "ix_attest_field_values_words", "attest_field_values", ["value_words"],
        schema=schema, postgresql_using="gin", postgresql_where=current,
    )  # fmt: skip
    op.create_table(
        "attest_field_watermarks",
        sa.Column("site_id", sa.String(128), primary_key=True),
        sa.Column("logbook", sa.String(64), primary_key=True),
        sa.Column("indexed_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("version", sa.String(32), nullable=False),
        schema=schema,
    )


def downgrade() -> None:
    # A projection holds no evidence: both tables rebuild from the sealed records.
    schema = op.get_context().version_table_schema
    op.drop_table("attest_field_watermarks", schema=schema)
    op.drop_table("attest_field_values", schema=schema)
