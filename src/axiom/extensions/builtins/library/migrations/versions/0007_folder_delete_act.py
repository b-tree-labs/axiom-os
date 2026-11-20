# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Deleting a folder takes what it holds, as one act with one undo (prd-sharing-and-access SHR-R4.5).

Expand only (ADR-182): one nullable column, ``deleted_with``, on ``library_items``
and on ``library_folders``. It names the folder delete that sent a row to Deleted
files, so restoring that folder restores exactly what it took. A row written
before this revision reads as deleted on its own, which is what it was.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def _schema() -> str:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("library migrations need version_table_schema (ADR-052)")
    return schema


def upgrade() -> None:
    S = _schema()
    for table in ("library_items", "library_folders"):
        op.add_column(table, sa.Column("deleted_with", sa.String(36), nullable=True), schema=S)


def downgrade() -> None:
    S = _schema()
    for table in ("library_folders", "library_items"):
        op.drop_column(table, "deleted_with", schema=S)
