# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Initial attest schema and its append-only guard (ADR-143).

Tables:

- ``attest_records``: signed records, one chain per (site, book). Append only.
- ``attest_chain_heads``: the head of each chain, locked while a record is added.
- ``attest_drafts``: what software proposed and people filled in. Mutable.
- ``attest_presentations``: exactly what a person was shown. Append only.
- ``attest_anchors``: signed Merkle roots over the book heads. Append only.

The guard is a trigger, so it holds for every client, the table owner
included: UPDATE and DELETE fail per row and TRUNCATE fails per statement.
An owner can still disable a trigger. That is what the chain is for: an edit
that gets past the guard breaks the digest, and verification names the record.

Every op names the schema, read from ``version_table_schema``, because
``engine_for`` does not set ``search_path``.

Revision ID: 0001
Revises:
Create Date: 2026-09-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APPEND_ONLY = ("attest_records", "attest_presentations", "attest_anchors")


def _schema() -> str:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    return schema


def upgrade() -> None:
    schema = _schema()
    tz = sa.DateTime(timezone=True)

    op.create_table(
        "attest_records",
        sa.Column("attestation_id", sa.String(36), primary_key=True),
        sa.Column("site_id", sa.String(128), nullable=False, index=True),
        sa.Column("book", sa.String(64), nullable=False, index=True),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False, unique=True),
        sa.Column("prev_digest", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("meaning", sa.String(32), nullable=False),
        sa.Column("entry_type", sa.String(64), nullable=True),
        sa.Column("signer", sa.String(256), nullable=False, index=True),
        sa.Column("occurred_at", tz, nullable=False),
        sa.Column("recorded_at", tz, nullable=False),
        sa.Column("record", JSONB(), nullable=False),
        sa.UniqueConstraint("site_id", "book", "seq"),
        schema=schema,
    )
    op.create_table(
        "attest_chain_heads",
        sa.Column("site_id", sa.String(128), primary_key=True),
        sa.Column("book", sa.String(64), primary_key=True),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        schema=schema,
    )
    op.create_table(
        "attest_drafts",
        sa.Column("draft_id", sa.String(36), primary_key=True),
        sa.Column("site_id", sa.String(128), nullable=False, index=True),
        sa.Column("book", sa.String(64), nullable=False),
        sa.Column("entry_type", sa.String(64), nullable=False),
        sa.Column("meaning", sa.String(32), nullable=False),
        sa.Column("content", JSONB(), nullable=False),
        sa.Column("provenance", JSONB(), nullable=False),
        sa.Column("origin", sa.String(128), nullable=False),
        sa.Column("for_principal", sa.String(256), nullable=False, index=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", tz, nullable=False),
        sa.Column("attestation_id", sa.String(36), nullable=True),
        schema=schema,
    )
    op.create_table(
        "attest_presentations",
        sa.Column("presentation_id", sa.String(36), primary_key=True),
        sa.Column("draft_id", sa.String(36), nullable=False, index=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("statement", JSONB(), nullable=False),
        sa.Column("rendered", sa.Text(), nullable=False),
        sa.Column("modality", sa.String(32), nullable=False),
        sa.Column("created_at", tz, nullable=False),
        schema=schema,
    )
    op.create_table(
        "attest_anchors",
        sa.Column("anchor_id", sa.String(36), primary_key=True),
        sa.Column("site_id", sa.String(128), nullable=False, index=True),
        sa.Column("created_at", tz, nullable=False),
        sa.Column("root", sa.String(64), nullable=False),
        sa.Column("heads", JSONB(), nullable=False),
        sa.Column("node_sig", JSONB(), nullable=False),
        schema=schema,
    )

    op.execute(
        f"""
        CREATE FUNCTION "{schema}".attest_refuse_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'attest: % is append-only; % refused', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END
        $$
        """
    )
    for table in APPEND_ONLY:
        op.execute(
            f'CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON "{schema}".{table} '
            f'FOR EACH ROW EXECUTE FUNCTION "{schema}".attest_refuse_change()'
        )
        op.execute(
            f'CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON "{schema}".{table} '
            f'FOR EACH STATEMENT EXECUTE FUNCTION "{schema}".attest_refuse_change()'
        )


def downgrade() -> None:
    # Dropping these tables would destroy signed evidence. Taking a node back
    # past this revision is an export followed by a deliberate, manual drop.
    raise RuntimeError(
        "attest 0001 cannot be downgraded: it would delete signed records. "
        "Export the books (axi attest export), then drop the schema by hand."
    )
