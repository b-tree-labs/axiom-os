# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Alembic environment for the ``vault`` schema (per ADR-052 §D3)."""

from __future__ import annotations

from alembic import context

from axiom.extensions.builtins.vault.db_models import Base
from axiom.infra.db import engine_for

target_metadata = Base.metadata
EXTENSION_NAME = "vault"


def run_migrations_offline() -> None:
    engine, schema = engine_for(EXTENSION_NAME)
    context.configure(
        url=str(engine.url),
        target_metadata=target_metadata,
        version_table_schema=schema,
        include_schemas=True,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine, schema = engine_for(EXTENSION_NAME)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=schema,
            include_schemas=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
