# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Confirmation evidence (ADR-144).

- ``attest_presentations`` gains the presenter that produced it, every
  rendered density, the speakable text, and when it expires. Existing rows
  keep NULLs: they were CLI presentations with one rendered form.
- ``attest_responses``: each answer a person gave to a presentation, append
  only. ``hold`` and ``ask`` are evidence too, not just ``sign``.
- ``attest_records.response_id``: the response a record was signed on, unique,
  so one answer can never sign twice.

Adding columns is DDL, which the row triggers do not see; no stored row changes.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-30
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
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    return schema


def upgrade() -> None:
    schema = _schema()
    tz = sa.DateTime(timezone=True)
    for column in (
        sa.Column("presenter", sa.String(128), nullable=True),
        sa.Column("forms", JSONB(), nullable=True),
        sa.Column("speakable", sa.Text(), nullable=True),
        sa.Column("expires_at", tz, nullable=True),
    ):
        op.add_column("attest_presentations", column, schema=schema)

    op.create_table(
        "attest_responses",
        sa.Column("response_id", sa.String(36), primary_key=True),
        sa.Column("presentation_id", sa.String(36), nullable=False, index=True),
        sa.Column("principal", sa.String(256), nullable=False),
        sa.Column("answer", sa.String(8), nullable=False),
        sa.Column("via", sa.String(32), nullable=False),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("audio_sha256", sa.String(64), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("console_id", sa.String(128), nullable=True),
        sa.Column("correction", JSONB(), nullable=True),
        sa.Column("at", tz, nullable=False),
        schema=schema,
    )
    op.execute(
        f"CREATE TRIGGER attest_responses_append_only BEFORE UPDATE OR DELETE "
        f'ON "{schema}".attest_responses FOR EACH ROW '
        f'EXECUTE FUNCTION "{schema}".attest_refuse_change()'
    )
    op.execute(
        f"CREATE TRIGGER attest_responses_no_truncate BEFORE TRUNCATE "
        f'ON "{schema}".attest_responses FOR EACH STATEMENT '
        f'EXECUTE FUNCTION "{schema}".attest_refuse_change()'
    )

    op.add_column(
        "attest_records", sa.Column("response_id", sa.String(36), nullable=True), schema=schema
    )
    op.create_unique_constraint(
        "attest_records_response_id_key", "attest_records", ["response_id"], schema=schema
    )


def downgrade() -> None:
    raise RuntimeError(
        "attest 0002 cannot be downgraded: it would delete recorded responses. "
        "Export the books (axi attest export), then drop the schema by hand."
    )
