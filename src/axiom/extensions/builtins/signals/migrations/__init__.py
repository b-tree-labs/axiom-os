# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Database migrations for Neut Sense using Alembic + SQLAlchemy.

This package manages PostgreSQL + pgvector schema migrations with SQLAlchemy ORM.

Quick Start:
    # Apply all pending migrations
    axi signal db migrate upgrade head

    # Check current revision
    axi signal db migrate current

    # Create a new migration (autogenerate from model changes)
    axi signal db migrate revision --autogenerate -m "add feature"

Programmatic Usage:
    from axiom.extensions.builtins.signals.migrations import run_migrations, check_migrations

    # Apply migrations
    run_migrations("upgrade", "head")

    # Check status
    status = check_migrations()
    if not status["up_to_date"]:
        print(f"Pending migrations: {status['pending']}")
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

# Migration directory
MIGRATIONS_DIR = Path(__file__).parent
ALEMBIC_INI = MIGRATIONS_DIR / "alembic.ini"

# Default database URL
DEFAULT_DB_URL = "postgresql://axiom:axiom@localhost:5432/axiom_db"


def get_db_url() -> str:
    """Get database URL from environment or default."""
    return os.environ.get("AXIOM_DB_URL", DEFAULT_DB_URL)


def get_alembic_config():
    """Get Alembic configuration."""
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # Set absolute version_locations so Alembic finds scripts regardless of
    # where the process is invoked from (the relative "versions" in alembic.ini
    # may not resolve correctly when script_location is overridden).
    config.set_main_option("version_locations", str(MIGRATIONS_DIR / "versions"))
    config.set_main_option("sqlalchemy.url", get_db_url())
    return config


def run_migrations(command: str, revision: str = "head", message: str = "", autogenerate: bool = False) -> bool:
    """Run Alembic migrations programmatically.

    Args:
        command: Alembic command (upgrade, downgrade, current, history, revision)
        revision: Target revision (default: "head")
        message: Message for new revision (required for 'revision' command)
        autogenerate: Auto-detect model changes for 'revision' command

    Returns:
        True if successful
    """
    from alembic import command as alembic_cmd

    config = get_alembic_config()

    try:
        if command == "upgrade":
            alembic_cmd.upgrade(config, revision)
        elif command == "downgrade":
            alembic_cmd.downgrade(config, revision)
        elif command == "current":
            alembic_cmd.current(config, verbose=True)
        elif command == "history":
            alembic_cmd.history(config, verbose=True)
        elif command == "heads":
            alembic_cmd.heads(config, verbose=True)
        elif command == "stamp":
            alembic_cmd.stamp(config, revision)
        elif command == "revision":
            alembic_cmd.revision(config, message=message, autogenerate=autogenerate)
        else:
            raise ValueError(f"Unknown command: {command}")
        return True
    except Exception as e:
        print(f"Migration error: {e}")
        return False


def get_current_revision() -> str | None:
    """Get the current database revision.

    Returns:
        Current revision ID, or None if database is not migrated
    """
    try:
        engine = create_engine(get_db_url())

        with engine.connect() as conn:
            # Check if alembic_version table exists
            inspector = inspect(engine)
            if "alembic_version" not in inspector.get_table_names():
                return None

            result = conn.execute(text("SELECT version_num FROM alembic_version"))
            row = result.fetchone()
            return row[0] if row else None

    except Exception:
        return None


def get_pending_migrations() -> list[str]:
    """Get list of pending migrations.

    Returns:
        List of pending revision IDs
    """
    from alembic.script import ScriptDirectory

    config = get_alembic_config()
    script = ScriptDirectory.from_config(config)

    # Get all available revisions (from base to head)
    all_revisions = []
    for rev in script.walk_revisions():
        all_revisions.append(rev.revision)
    all_revisions.reverse()  # Base first

    # Get current revision
    current = get_current_revision()

    if current is None:
        return all_revisions

    # Find pending revisions (after current)
    try:
        current_idx = all_revisions.index(current)
        return all_revisions[current_idx + 1:]
    except ValueError:
        # Current revision not in list (shouldn't happen)
        return []


def check_migrations() -> dict:
    """Check migration status.

    Returns:
        Dict with status information:
        - current: Current revision
        - head: Head revision
        - pending: Number of pending migrations
        - up_to_date: True if all migrations applied
        - connected: True if database is reachable
    """
    from alembic.script import ScriptDirectory

    config = get_alembic_config()
    script = ScriptDirectory.from_config(config)

    current = get_current_revision()
    head = script.get_current_head()
    pending = get_pending_migrations()

    # Test connection
    connected = False
    try:
        engine = create_engine(get_db_url())
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            connected = True
    except Exception:
        pass

    return {
        "current": current,
        "head": head,
        "pending": len(pending),
        "pending_revisions": pending,
        "up_to_date": current == head,
        "connected": connected,
    }


def verify_schema() -> dict:
    """Verify database schema matches expected state.

    Returns:
        Dict with verification results
    """
    try:
        engine = create_engine(get_db_url())
        inspector = inspect(engine)

        expected_tables = ["signals", "media", "participants", "people", "alembic_version"]
        actual_tables = inspector.get_table_names()
        missing_tables = [t for t in expected_tables if t not in actual_tables]

        # Check pgvector extension
        has_pgvector = False
        with engine.connect() as conn:
            result = conn.execute(text(
                "SELECT EXISTS (SELECT FROM pg_extension WHERE extname = 'vector')"
            ))
            has_pgvector = result.scalar()

        return {
            "valid": len(missing_tables) == 0 and has_pgvector,
            "missing_tables": missing_tables,
            "has_pgvector": has_pgvector,
            "tables_found": [t for t in expected_tables if t in actual_tables],
        }

    except Exception as e:
        return {"error": str(e), "valid": False, "connected": False}


def ensure_pgvector_extension() -> bool:
    """Ensure pgvector extension is installed.

    Returns:
        True if extension is available
    """
    try:
        engine = create_engine(get_db_url())
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        return True
    except Exception as e:
        print(f"Error enabling pgvector: {e}")
        return False
