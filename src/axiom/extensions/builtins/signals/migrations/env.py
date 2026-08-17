# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Alembic environment configuration for Neut Sense database migrations.

This module configures Alembic to run migrations against PostgreSQL + pgvector
using SQLAlchemy ORM models.

Connection URL is determined by:
  1. AXIOM_DB_URL environment variable
  2. Default local K3D URL: postgresql://axiom:axiom@localhost:5432/axiom_db

Autogenerate support:
  alembic revision --autogenerate -m "description"

This will compare db_models.py against the database and generate migrations.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure src/ is on the path so absolute imports work whether alembic is
# invoked via CLI or programmatically from run_migrations()
from axiom import REPO_ROOT as _REPO_ROOT

sys.path.insert(0, str(_REPO_ROOT / "src"))

# Absolute import — relative import fails when alembic runs env.py outside
# the package context (e.g. programmatic invocation via run_migrations())
from axiom.extensions.builtins.signals.db_models import Base

# Alembic Config object
config = context.config

# Set up logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for autogenerate
target_metadata = Base.metadata


def get_url() -> str:
    """Get database URL from environment or default."""
    return os.environ.get(
        "AXIOM_DB_URL",
        "postgresql://axiom:axiom@localhost:5432/axiom_db"
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This generates SQL scripts without connecting to the database.
    Useful for reviewing migrations before applying.
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Creates a connection and runs migrations directly against the database.
    """
    # Configure SQLAlchemy engine
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            # Include pgvector types in autogenerate
            include_schemas=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
