# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An index for the field index's ``datetime`` values (ADR-268 D2 and D4, phase 2).

A ``datetime`` filter compares ``value_at``, an inclusive range of instants. Like the text and
number indexes of migration 0015, this one starts with the labels the door checks (site, log,
field) and ends with the entry's sequence number, partial on ``current``, so a time filter is an
index-prefix predicate too.

Expand only (ADR-182): one new index. No row is written and no digest changes.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("attest migrations need version_table_schema (ADR-052)")
    op.create_index(
        "ix_attest_field_values_at", "attest_field_values",
        ["site_id", "logbook", "field", "value_at", "entry_seq"],
        schema=schema, postgresql_where=sa.text("current"),
    )  # fmt: skip


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_index("ix_attest_field_values_at", table_name="attest_field_values", schema=schema)
