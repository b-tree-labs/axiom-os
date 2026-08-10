# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Schema-per-extension database primitive — ADR-052.

Extensions never see a DSN, the connection pool, or schema-creation DDL.
They consume a single scoped session:

    from axiom.infra.db import session_for

    with session_for("expman") as s:
        s.add(sample)
        s.commit()

The provider owns the shared Engine/pool, computes a safe Postgres schema
name from the extension name, ensures the schema exists (idempotent), and
sets ``search_path`` so unqualified table names resolve to the extension's
own schema.

Per ADR-050 the platform vocabulary is ``tenant`` (data-owner / partition
within an extension's data) and ``site`` (physical install). This module
delivers extension-level isolation; the within-extension tenancy menu
(single / row-level ``tenant_id`` / schema-per-tenant — ADR-052 §D4)
sits on top.
"""

from __future__ import annotations

import os
import re
import threading
from contextlib import contextmanager
from typing import Iterator, Optional, Tuple

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_DB_URL = "postgresql://axiom:axiom@localhost:5432/axiom_db"

_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker] = None
_engine_lock = threading.Lock()

# Postgres unquoted identifiers must match [a-z_][a-z0-9_]*. We normalize
# anything outside that set to underscore so we never need to quote at
# call sites; see normalize_extension_name.
_UNSAFE = re.compile(r"[^a-z0-9_]")
# NAMEDATALEN - 1; Postgres truncates anything longer.
_MAX_IDENT = 63


def normalize_extension_name(name: str) -> str:
    """Coerce an extension name into a safe Postgres schema identifier.

    Lowercase, hyphens → underscores, anything else → underscores, leading
    underscores stripped, length-capped at 63 chars. All-unsafe input falls
    back to ``"ext"`` so the result is always a valid identifier; empty or
    whitespace-only input raises.

    Test isolation hook: when ``AXIOM_TEST_SCHEMA_SUFFIX`` is set in env,
    its value is appended to the normalized name (and re-normalized so a
    malformed worker id stays a valid identifier). The conftest sets this
    per pytest-xdist worker so persisted-state tests get worker-scoped
    schemas (``vault_gw0``, ``vault_gw1``, …) and parallel teardowns
    can't TRUNCATE each other's rows. The var is unset in production —
    behavior is unchanged when absent.
    """
    if not name or not name.strip():
        raise ValueError("extension name cannot be empty")
    s = name.strip().lower().replace("-", "_")
    s = _UNSAFE.sub("_", s)
    s = s.lstrip("_") or "ext"

    suffix = os.environ.get("AXIOM_TEST_SCHEMA_SUFFIX", "")
    if suffix:
        # Run the suffix through the same coercion so a worker id like
        # "gw-3" becomes "gw_3" rather than producing an unsafe ident.
        suffix_norm = _UNSAFE.sub("_", suffix.lower().replace("-", "_"))
        s = s + suffix_norm

    return s[:_MAX_IDENT]


def get_engine() -> Engine:
    """Return the process-wide shared SQLAlchemy Engine.

    Lazy-built from ``AXIOM_DB_URL`` on first call; every extension's
    ``session_for(...)`` rides the same Engine + pool.
    """
    global _engine, _session_factory
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                url = os.environ.get("AXIOM_DB_URL", DEFAULT_DB_URL)
                _engine = create_engine(url, future=True)
                _session_factory = sessionmaker(
                    bind=_engine, future=True, expire_on_commit=False
                )
    return _engine


def ensure_schema(engine: Engine, extension_name: str) -> str:
    """Idempotently ``CREATE SCHEMA IF NOT EXISTS`` for the extension.

    Returns the normalized schema name. Safe to call on every session —
    the second call is a no-op at the DB layer.
    """
    schema = normalize_extension_name(extension_name)
    # normalize_extension_name guarantees [a-z0-9_], so direct
    # interpolation is safe; param binding doesn't work for identifiers.
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    return schema


@contextmanager
def session_for(extension_name: str) -> Iterator[Session]:
    """Yield a Session scoped to *extension_name*'s schema.

    On enter: ensures the schema exists and sets ``search_path`` to
    ``"<schema>, public"`` on this session's connection so unqualified
    table names resolve to the extension's own schema.

    Commit/rollback is the caller's call. We don't auto-commit on exit —
    that would mask transactional intent. Typical use::

        with session_for("expman") as s:
            do_work(s)
            s.commit()
    """
    get_engine()  # ensures _session_factory is built
    schema = ensure_schema(_engine, extension_name)  # type: ignore[arg-type]
    assert _session_factory is not None  # narrowed by get_engine()
    session = _session_factory()
    try:
        session.execute(text(f'SET search_path TO "{schema}", public'))
        yield session
    finally:
        session.close()


def engine_for(extension_name: str) -> Tuple[Engine, str]:
    """Return ``(shared engine, schema)`` for an extension.

    Intended for an extension's Alembic ``env.py``::

        from axiom.infra.db import engine_for

        connectable, schema = engine_for("expman")
        with connectable.connect() as conn:
            context.configure(
                connection=conn,
                target_metadata=target_metadata,
                version_table_schema=schema,
                include_schemas=True,
            )
            with context.begin_transaction():
                context.run_migrations()

    Ensures the schema exists before returning so the migration env has a
    home to write its ``alembic_version`` table into.
    """
    engine = get_engine()
    schema = ensure_schema(engine, extension_name)
    return engine, schema


__all__ = [
    "DEFAULT_DB_URL",
    "ensure_schema",
    "engine_for",
    "get_engine",
    "normalize_extension_name",
    "session_for",
]
