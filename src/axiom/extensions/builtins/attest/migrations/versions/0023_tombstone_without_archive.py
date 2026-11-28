# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A tombstone may name no archive (ADR-290 D8, erasing under storage pressure).

Under storage pressure the schedule erases a sealed period past its declared ``delete_after_days``
that was never archived; its tombstone records that there was no archive, and its working row has
no ``archive_id``.

Expand only (ADR-182): one column becomes nullable. No row is written, and a release that does not
know the new path never reads a null (it never creates one).

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    op.alter_column(
        "attest_tombstones", "archive_id", existing_type=sa.String(36), nullable=True, schema=schema
    )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.alter_column(
        "attest_tombstones",
        "archive_id",
        existing_type=sa.String(36),
        nullable=False,
        schema=schema,
    )
