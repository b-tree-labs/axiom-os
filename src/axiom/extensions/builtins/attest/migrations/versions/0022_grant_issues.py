# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Signing grants issued and revoked (ADR-146, ADR-250 D4, RW-G1).

A signing grant was a bearer token the store never saw until it signed, so it could be neither
listed nor withdrawn. ``attest_grant_issues`` holds one row per grant minted (what it is for,
never the token), and ``attest_grant_revocations`` one row per grant withdrawn before use; the
signing path refuses a revoked grant. Both append-only, like ``attest_grant_uses``.

Expand only (ADR-182): two new tables. No row of an existing table is written.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def _append_only(schema: str, table: str) -> None:
    for trigger, timing in (
        ("append_only", "BEFORE UPDATE OR DELETE ON {t} FOR EACH ROW"),
        ("no_truncate", "BEFORE TRUNCATE ON {t} FOR EACH STATEMENT"),
    ):
        op.execute(
            f"CREATE TRIGGER {table}_{trigger} "
            + timing.format(t=f'"{schema}".{table}')
            + f' EXECUTE FUNCTION "{schema}".attest_refuse_change()'
        )


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    tz = sa.DateTime(timezone=True)
    op.create_table(
        "attest_grant_issues",
        sa.Column("grant_id", sa.String(36), primary_key=True),
        sa.Column("presentation_id", sa.String(36), nullable=False),
        sa.Column("site_id", sa.String(128), nullable=False),
        sa.Column("logbook", sa.String(64), nullable=False),
        sa.Column("meaning", sa.String(64), nullable=False),
        sa.Column("digest", sa.String(128), nullable=False),
        sa.Column("principal", sa.String(256), nullable=False),
        sa.Column("issuer", sa.String(256), nullable=False),
        sa.Column("issued_at", tz, nullable=False),
        sa.Column("expires_at", tz, nullable=False),
        schema=schema,
    )
    op.create_index("ix_attest_grant_issues_expires", "attest_grant_issues", ["expires_at"],
                    schema=schema)  # fmt: skip
    _append_only(schema, "attest_grant_issues")
    op.create_table(
        "attest_grant_revocations",
        sa.Column("grant_id", sa.String(36), primary_key=True),
        sa.Column("revoked_by", sa.String(256), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("revoked_at", tz, nullable=False),
        schema=schema,
    )
    _append_only(schema, "attest_grant_revocations")


def downgrade() -> None:
    raise RuntimeError(
        "attest 0022 cannot be downgraded: forgetting a revocation would let a withdrawn grant sign."
    )
