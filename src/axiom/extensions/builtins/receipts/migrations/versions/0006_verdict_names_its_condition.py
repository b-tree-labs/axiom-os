# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A decision says what KIND of trouble it was about.

`calibrate` is specified as a measurement per ``(solver, condition)`` —
spec-case-construct §3 — because "how good is this decider's judgement" is
only answerable per kind of trouble. A decider who is excellent at silent
reporters and poor at contradicted evidence has no meaningful single
number, and averaging the two licenses them at the thing they are worst
at.

The record could not answer that. It carries ``case_id``, which names the
ENTITY and site, and nothing that names the condition. Grouping by case
id measures "how did this go for node-a", which is not the question.

So the condition is captured at decision time as ``"<claim_kind>|<status>"``
of the case's root claim — the same pair that keys the condition table.
Captured rather than derived later, for the same reason ``decider_label``
is: a case recomposed next week may have a different root, and a decision
record must say what was actually in front of the person.

Nullable, no backfill. Older rows genuinely do not know their condition
and inventing one would put a measurement on a foundation of guesses —
calibration over a fabricated key is worse than calibration refused.
Those rows are excluded from the measurement and counted as excluded.

Every op names ``schema=SCHEMA`` explicitly (see 0001).

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

SCHEMA = "receipts"


def upgrade() -> None:
    op.add_column(
        "case_verdict",
        sa.Column("condition", sa.String(length=120), nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_case_verdict_condition",
        "case_verdict",
        ["condition"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_case_verdict_condition", table_name="case_verdict", schema=SCHEMA)
    op.drop_column("case_verdict", "condition", schema=SCHEMA)
