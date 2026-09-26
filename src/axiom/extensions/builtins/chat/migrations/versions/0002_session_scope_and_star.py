# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Site/tenant scope + starred on chat sessions.

The initial store was principal-scoped only, which cannot serve a multi-tenant
web surface: reads there must be scoped to a site (physical install) and a
tenant (the account the web filters by — appkit's ``account_id``). ``starred``
is the user pin, a column so listing can filter/sort on it and a star survives
an append-save.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SCHEMA = "chat"


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("site_id", sa.String(128), nullable=False, server_default=""),
        schema=SCHEMA,
    )
    op.add_column(
        "chat_sessions",
        sa.Column("tenant_id", sa.String(256), nullable=False, server_default=""),
        schema=SCHEMA,
    )
    op.add_column(
        "chat_sessions",
        sa.Column("starred", sa.Boolean(), nullable=False, server_default=sa.false()),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_chat_sessions_site_id", "chat_sessions", ["site_id"], schema=SCHEMA
    )
    op.create_index(
        "ix_chat_sessions_tenant_id", "chat_sessions", ["tenant_id"], schema=SCHEMA
    )
    # A shared surface lists "this site+tenant's most recent"; the index carries
    # the scope axes and the sort column together so the read never scans wide.
    op.create_index(
        "ix_chat_sessions_scope_updated",
        "chat_sessions",
        ["site_id", "tenant_id", "updated_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_chat_sessions_scope_updated", "chat_sessions", schema=SCHEMA)
    op.drop_index("ix_chat_sessions_tenant_id", "chat_sessions", schema=SCHEMA)
    op.drop_index("ix_chat_sessions_site_id", "chat_sessions", schema=SCHEMA)
    op.drop_column("chat_sessions", "starred", schema=SCHEMA)
    op.drop_column("chat_sessions", "tenant_id", schema=SCHEMA)
    op.drop_column("chat_sessions", "site_id", schema=SCHEMA)
