# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Draft subjects: what a record is about (logbook unit X3).

``attest_drafts.subjects`` carries the record's subjects until it is signed; each is
``{kind, value, role?}`` (an approval decision's subject is ``{kind: "action", value: <id>}``).
The signed record copies them into its body. Expand only (ADR-182): one nullable column, no row
of ``attest_records`` is rewritten, and no existing digest changes, because a statement carries
``subjects`` only when it has some.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    op.add_column("attest_drafts", sa.Column("subjects", JSONB, nullable=True), schema=schema)


def downgrade() -> None:
    raise RuntimeError(
        "attest 0012 cannot be downgraded: dropping subjects would cut an open decision draft "
        "from the action it decides."
    )
