# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Imported records' source references, one live reference per entry (Axiom #1804, ADR-262).

``attest_import_refs`` holds one row per ``import`` record: the source's own id for the entry
(``source_ref``), its batch and its record. The unique partial index on ``(site_id, logbook,
source_ref) WHERE withdrawn_at IS NULL`` is what makes an import idempotent under two concurrent
commits, and lets a withdrawn batch's entries be imported again. It cannot be a predicate on
``attest_records``: that table is append only, and a batch is withdrawn later, by a record.

This table is working state beside the chain, not evidence: the import and withdrawal records
on the chain are the truth, and a withdrawal sets ``withdrawn_at`` in the same transaction as
its record. A page sorted by when entries happened reads 0010's (site, logbook, occurred_at)
index. Expand only (ADR-182): one table, two indexes, no row rewritten, no digest changed.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    op.create_table(
        "attest_import_refs",
        sa.Column("attestation_id", sa.String(36), primary_key=True),
        sa.Column("site_id", sa.String(128), nullable=False),
        sa.Column("logbook", sa.String(64), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("batch_id", sa.String(36), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        schema=schema,
    )
    op.create_index(
        "attest_import_refs_live",
        "attest_import_refs",
        ["site_id", "logbook", "source_ref"],
        unique=True,
        schema=schema,
        postgresql_where=sa.text("withdrawn_at IS NULL"),
    )
    op.create_index("attest_import_refs_batch", "attest_import_refs", ["batch_id"], schema=schema)


def downgrade() -> None:
    raise RuntimeError(
        "attest 0014 cannot be downgraded: it would forget which imported entries are on the "
        "chain, and the next import would write them twice."
    )
