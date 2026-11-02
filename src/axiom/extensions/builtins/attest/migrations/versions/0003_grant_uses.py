# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Signing-grant uses (ADR-146).

``attest_grant_uses`` holds one row per grant that signed. The primary key is
the grant id, so a second use fails inside the signing transaction. Append
only, like the other evidence tables.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _schema() -> str:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    return schema


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "attest_grant_uses",
        sa.Column("grant_id", sa.String(36), primary_key=True),
        sa.Column("presentation_id", sa.String(36), nullable=False),
        sa.Column("principal", sa.String(256), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )
    for trigger, timing in (
        ("append_only", "BEFORE UPDATE OR DELETE ON {t} FOR EACH ROW"),
        ("no_truncate", "BEFORE TRUNCATE ON {t} FOR EACH STATEMENT"),
    ):
        op.execute(
            f"CREATE TRIGGER attest_grant_uses_{trigger} "
            + timing.format(t=f'"{schema}".attest_grant_uses')
            + f' EXECUTE FUNCTION "{schema}".attest_refuse_change()'
        )


def downgrade() -> None:
    raise RuntimeError(
        "attest 0003 cannot be downgraded: forgetting used grants would let one sign again."
    )
