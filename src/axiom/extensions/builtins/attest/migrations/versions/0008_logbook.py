# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Rename "book" to "logbook" (ADR-150).

"Book" is the platform's library / artifact repository. Attestation's
append-only record of signed entries is a logbook. Revisions 0001-0005
created ``book`` columns on records, chain heads and drafts; this renames
them, and any index whose name still carries ``book``. Rename only: no row
changes, so the append-only triggers do not fire.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

TABLES = ("attest_records", "attest_chain_heads", "attest_drafts")


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    for table in TABLES:
        op.alter_column(table, "book", new_column_name="logbook", schema=schema)
    bind = op.get_bind()
    names = bind.execute(
        sa.text(
            "SELECT indexname FROM pg_indexes WHERE schemaname = :s AND indexname LIKE '%book%' "
            "AND indexname NOT LIKE '%logbook%'"
        ),
        {"s": schema},
    ).scalars()
    for name in list(names):
        new = name.replace("book", "logbook")
        op.execute(f'ALTER INDEX "{schema}"."{name}" RENAME TO "{new}"')


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    for table in TABLES:
        op.alter_column(table, "logbook", new_column_name="book", schema=schema)
