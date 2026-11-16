# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A decision that RAN something says what it ran and how it went.

Before this, "this executed, here is what came back" had nowhere durable
to live. The audit hash chain keeps a digest of the inputs and a boolean;
the action provenance ledger keeps per-candidate outcomes and an undo
handle; the only structure that held a return value at all — the
approval queue's ``Action.result`` — is purged the moment the action
resolves, because that queue is a work list rather than a record.

A decision receipt whose action is invisible is half a receipt, and the
half it is missing is the half that makes the next occurrence cheaper.

Nullable with no backfill, for the usual reason: decisions taken before
this column existed genuinely ran nothing, and ``NULL`` says exactly
that. ``ran_ok`` is deliberately tri-state — NULL is "nothing ran",
which is a different fact from "it ran and failed".

Every op names ``schema=SCHEMA`` explicitly (see 0001).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

SCHEMA = "receipts"


def upgrade() -> None:
    op.add_column(
        "case_verdict",
        sa.Column("ran_capability", sa.String(128), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "case_verdict",
        sa.Column("ran_ok", sa.Boolean, nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "case_verdict",
        sa.Column("ran_detail", sa.Text, nullable=False, server_default=""),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("case_verdict", "ran_detail", schema=SCHEMA)
    op.drop_column("case_verdict", "ran_ok", schema=SCHEMA)
    op.drop_column("case_verdict", "ran_capability", schema=SCHEMA)
