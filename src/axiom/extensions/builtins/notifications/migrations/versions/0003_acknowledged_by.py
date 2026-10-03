# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Record WHO acknowledged an alert, not only when.

`acknowledged_at` alone is unambiguous for exactly as long as one rule holds:
only the recipient may acknowledge, so the row's recipient IS the acknowledger.
Group recipients end that rule (PRD §5.11) — a respond-once group has many
possible answerers and the timestamp then means "somebody, at some point",
which nobody can act on.

Added BEFORE there are rows to backfill. Backfilling acknowledgements whose
acknowledger can no longer be determined is not a migration; it is a permanent
gap in the record with a date range on it.

"Who MAY answer" and "who DID answer" are different questions. Authority is
GUARD's, keyed on the capability. The row's job is only the second.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SCHEMA = "notifications"


def upgrade() -> None:
    op.add_column(
        "notifications_inbox",
        sa.Column("acknowledged_by", sa.String(), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("notifications_inbox", "acknowledged_by", schema=SCHEMA)
