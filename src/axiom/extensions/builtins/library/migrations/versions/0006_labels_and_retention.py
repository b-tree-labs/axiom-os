# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""MVP-22: an export-control label and a retention class on every item; sample items.

Expand only (ADR-182): three nullable columns on ``library_items``. A row
written before this revision reads as *unlabelled* (no confirmed label) and as
the default retention class, ``operational``, so nothing has to be backfilled
for the floors to hold. Every op names the schema read from
``version_table_schema`` (see 0001).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_COLUMNS = (
    #: ``governance.classification.ExportControlLabel.to_json()``: value, state, by, at, basis.
    ("ec_label", sa.JSON),
    #: ``governance.retention.RetentionClass``; NULL reads as the default (operational).
    ("retention_class", sa.String(16)),
    #: A sample item (first run): labelled "sample" everywhere, never retrieved or exported.
    ("sample", sa.Boolean),
)


def _schema() -> str:
    schema = op.get_context().version_table_schema
    if not schema:
        raise RuntimeError("library migrations need version_table_schema (ADR-052)")
    return schema


def upgrade() -> None:
    S = _schema()
    for name, type_ in _COLUMNS:
        op.add_column("library_items", sa.Column(name, type_, nullable=True), schema=S)


def downgrade() -> None:
    S = _schema()
    for name, _type in reversed(_COLUMNS):
        op.drop_column("library_items", name, schema=S)
