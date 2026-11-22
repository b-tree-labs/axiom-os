# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Attachments by content hash (ADR-251 D6, unit LD-4).

``attest_drafts.attachments`` holds the references a draft carries before it is signed
(``[{sha256, media_type, bytes, name?, rendition?, policy_version?, loss?}]``); signing copies
them into the record's ``evidence.attachments``, inside the signed bytes. The bytes themselves
are never in a row: they are blobs in the node's object store, keyed by their hash.

``attest_attachment_index`` is the labelled derived index of what was extracted from an
attachment: one row per text chunk or metadata part, carrying the entry's labels (site, log,
classification, export-control flag) and pointing at the record and the hash. A projection of
signed records and their blobs, rebuildable, never a source of truth. The label columns are NOT
NULL: a row without labels cannot be written.

Expand only (ADR-182): one nullable column and one new table. No signed row is read differently
and no digest changes.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    op.add_column("attest_drafts", sa.Column("attachments", JSONB(), nullable=True), schema=schema)
    op.create_table(
        "attest_attachment_index",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("attestation_id", sa.String(36), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("site_id", sa.String(128), nullable=False),
        sa.Column("logbook", sa.String(64), nullable=False),
        sa.Column("classification", sa.String(32), nullable=False),
        sa.Column("export_controlled", sa.Boolean(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("part", sa.String(16), nullable=False),
        sa.Column("chunk", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("meta", JSONB(), nullable=True),
        sa.Column("embedding", JSONB(), nullable=True),
        sa.Column("embed_model", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("attestation_id", "sha256", "part", "chunk",
                            name="uq_attest_attachment_index_part"),
        schema=schema,
    )  # fmt: skip
    op.create_index(
        "ix_attest_attachment_index_labels", "attest_attachment_index",
        ["site_id", "logbook", "attestation_id"], schema=schema,
    )  # fmt: skip
    op.create_index("ix_attest_attachment_index_sha", "attest_attachment_index", ["sha256"], schema=schema)


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_table("attest_attachment_index", schema=schema)
    op.drop_column("attest_drafts", "attachments", schema=schema)
