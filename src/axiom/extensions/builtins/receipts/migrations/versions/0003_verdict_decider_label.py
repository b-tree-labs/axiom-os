# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What the decider was CALLED when they decided.

``decider`` is the identity and stays the identity: a handle keyed on
the identity-provider subject, which for most providers is a GUID. That
is the right thing to key on and the wrong thing to show — a case page
reading "Acknowledged by @78f6cda0-4068-4c73-b8f7-53210aee4379" tells a
reader nothing about who decided.

The label is recorded ALONGSIDE the handle rather than looked up at read
time, because a directory lookup answers who the person is called
*today* while a decision record must say who they were called *then*.
Names change; the record does not.

Nullable with no backfill: rows written before this column existed
genuinely did not capture a label, and inventing one now would be
putting a guess into an append-only record. Those rows render by handle,
which is what they actually say.

Every op names ``schema=SCHEMA`` explicitly (see 0001).

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SCHEMA = "receipts"


def upgrade() -> None:
    op.add_column(
        "case_verdict",
        sa.Column("decider_label", sa.String(200), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("case_verdict", "decider_label", schema=SCHEMA)
