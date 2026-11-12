# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Search and paging at scale (logbook unit W3): a text-search projection of each signed record
and the indexes keyset paging and the record filters read.

``attest_records.search_vector`` is a generated ``tsvector`` over the signed content's title, body
and fields (``to_tsvector('simple', ...)``), derived from the sealed ``record`` and never written
by hand: an index as a labelled projection of the record (ADR-251), kept beside the chain until a
gold view of the chain exists for it to move to. No signed row is rewritten: a generated column
is computed by the database and the append-only triggers are not fired.

Indexes, expand only: a GIN index on ``search_vector``; btree indexes on (site, logbook,
entry_type, seq), (site, logbook, signer, seq) and (site, logbook, occurred_at), so each filter
pages by sequence inside its own index; and an expression index on the record's interval
(kind, number), which the interval filter and the intervals listing's counts read. The unique
constraint on (site, logbook, seq) from 0001 is the keyset index itself.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

SEARCH_EXPRESSION = (
    "to_tsvector('simple', coalesce(record->'content'->>'title', '') || ' ' || "
    "coalesce(record->'content'->>'body', '') || ' ' || coalesce(record->'content'->>'fields', ''))"
)


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    op.execute(
        f'ALTER TABLE "{schema}".attest_records ADD COLUMN search_vector tsvector '
        f"GENERATED ALWAYS AS ({SEARCH_EXPRESSION}) STORED"
    )
    op.create_index(
        "ix_attest_records_search",
        "attest_records",
        ["search_vector"],
        schema=schema,
        postgresql_using="gin",
    )
    op.create_index(
        "ix_attest_records_type_seq",
        "attest_records",
        ["site_id", "logbook", "entry_type", "seq"],
        schema=schema,
    )
    op.create_index(
        "ix_attest_records_signer_seq",
        "attest_records",
        ["site_id", "logbook", "signer", "seq"],
        schema=schema,
    )
    op.create_index(
        "ix_attest_records_occurred",
        "attest_records",
        ["site_id", "logbook", "occurred_at"],
        schema=schema,
    )
    op.create_index(
        "ix_attest_records_interval",
        "attest_records",
        [
            "site_id",
            "logbook",
            sa.text("(record->'interval'->>'kind')"),
            sa.text("((record->'interval'->>'number')::bigint)"),
        ],
        schema=schema,
    )


def downgrade() -> None:
    # The projection and its indexes hold no evidence: every one rebuilds from the sealed record.
    schema = op.get_context().version_table_schema
    for name in (
        "ix_attest_records_interval",
        "ix_attest_records_occurred",
        "ix_attest_records_signer_seq",
        "ix_attest_records_type_seq",
        "ix_attest_records_search",
    ):
        op.drop_index(name, table_name="attest_records", schema=schema)
    op.drop_column("attest_records", "search_vector", schema=schema)
