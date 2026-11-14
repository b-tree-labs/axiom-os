# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The sharing record: access grants, access settings, access requests (ADR-263 D10).

Three new tables and nothing else, so it only adds (ADR-182). No row is rewritten:
an object with no ``access_settings`` row is answered from its home's projection
(a Library item's ``visibility``), which is how every access a person has today
is kept (prd-sharing-and-access §8, SHR-A11).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_grants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("site", sa.String(64), nullable=False),
        sa.Column("object_kind", sa.String(16), nullable=False),
        sa.Column("object_id", sa.String(64), nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("granted_by", sa.String(200), nullable=False),
        sa.Column("granted_via", sa.String(16), nullable=False, server_default="share"),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(200), nullable=True),
        sa.Column("revoked_reason", sa.String(16), nullable=True),
        sa.Column("receipt_id", sa.String(), nullable=True),
        schema="authz",
    )
    op.create_index(
        "ix_access_grants_object", "access_grants", ["object_kind", "object_id"], schema="authz"
    )
    op.create_index("ix_access_grants_subject", "access_grants", ["site", "subject"], schema="authz")
    op.create_table(
        "access_settings",
        sa.Column("object_kind", sa.String(16), primary_key=True),
        sa.Column("object_id", sa.String(64), primary_key=True),
        sa.Column("site", sa.String(64), nullable=False),
        sa.Column("general", sa.String(16), nullable=False),
        sa.Column("general_role", sa.String(16), nullable=False, server_default="viewer"),
        sa.Column("editors_can_share", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("viewers_can_download", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_by", sa.String(200), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema="authz",
    )
    op.create_table(
        "access_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("site", sa.String(64), nullable=False),
        sa.Column("object_kind", sa.String(16), nullable=False),
        sa.Column("object_id", sa.String(64), nullable=False),
        sa.Column("requester", sa.String(200), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("state", sa.String(16), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(200), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("granted_role", sa.String(16), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        schema="authz",
    )
    op.create_index(
        "ix_access_requests_object",
        "access_requests",
        ["object_kind", "object_id", "state"],
        schema="authz",
    )
    op.create_index(
        "ix_access_requests_requester", "access_requests", ["site", "requester"], schema="authz"
    )


def downgrade() -> None:
    op.drop_index("ix_access_requests_requester", table_name="access_requests", schema="authz")
    op.drop_index("ix_access_requests_object", table_name="access_requests", schema="authz")
    op.drop_table("access_requests", schema="authz")
    op.drop_table("access_settings", schema="authz")
    op.drop_index("ix_access_grants_subject", table_name="access_grants", schema="authz")
    op.drop_index("ix_access_grants_object", table_name="access_grants", schema="authz")
    op.drop_table("access_grants", schema="authz")
