# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Per-person marks: the stars and opens behind Starred and Recent.

Expand only (ADR-182): one new table. Every op names the schema read from
``version_table_schema`` (see 0001).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-28
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
        raise RuntimeError("library migrations need version_table_schema (ADR-052)")
    return schema


def upgrade() -> None:
    S = _schema()
    op.create_table(
        "library_marks",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("site", sa.String(64), nullable=False),
        sa.Column("principal", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="item"),
        sa.Column("target", sa.String(255), nullable=False),
        sa.Column("starred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("open_count", sa.Integer, nullable=False, server_default="0"),
        schema=S,
    )
    op.create_index(
        "ux_library_marks_who_what",
        "library_marks",
        ["site", "principal", "kind", "target"],
        unique=True,
        schema=S,
    )
    op.create_index(
        "ix_library_marks_starred", "library_marks", ["site", "principal", "starred_at"], schema=S
    )
    op.create_index(
        "ix_library_marks_opened", "library_marks", ["site", "principal", "opened_at"], schema=S
    )


def downgrade() -> None:
    S = _schema()
    for index in (
        "ix_library_marks_opened",
        "ix_library_marks_starred",
        "ux_library_marks_who_what",
    ):
        op.drop_index(index, table_name="library_marks", schema=S)
    op.drop_table("library_marks", schema=S)
