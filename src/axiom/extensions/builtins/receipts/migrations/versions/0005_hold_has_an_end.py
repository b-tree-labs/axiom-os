# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A hold stops being a hold.

"Leave this alone" with no end is not a decision, it is a way of never
deciding: the case disappears from every list and nothing ever brings it
back. That was survivable while the button just said "Hold"; it is not
survivable now that the button has to say what holding will DO.

Nullable, and null means what it has always meant: held indefinitely.
Rows written before this column existed genuinely had no end, and
inventing one for them would be putting a decision in somebody's mouth.

Every op names ``schema=SCHEMA`` explicitly (see 0001).

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

SCHEMA = "receipts"


def upgrade() -> None:
    op.add_column(
        "case_verdict",
        sa.Column("hold_until", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("case_verdict", "hold_until", schema=SCHEMA)
