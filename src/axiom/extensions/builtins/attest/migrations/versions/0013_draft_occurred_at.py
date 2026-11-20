# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""When a draft's entry happened, if earlier than its signing (Axiom #1813).

``attest_drafts.occurred_at`` holds the time the person says the entry happened, until it is
signed; the signed record carries it as ``occurred_at`` beside the server's ``recorded_at``.
Expand only (ADR-182): one nullable column, no row of ``attest_records`` is rewritten, and no
existing digest changes, because a statement carries ``occurred_at`` only when one was given.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    op.add_column(
        "attest_drafts",
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        schema=schema,
    )


def downgrade() -> None:
    raise RuntimeError(
        "attest 0013 cannot be downgraded: dropping occurred_at would change the time an open "
        "draft says its entry happened."
    )
