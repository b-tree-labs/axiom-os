# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Test harness: bind PULSE's session provider to an in-memory SQLite DB.

Production uses ``session_for('schedule')`` (Postgres, ADR-052). For units we
inject a SQLite session so the fire loop runs without a live database. The
models are schema-unqualified, so ``create_all`` covers them on SQLite.
"""

from __future__ import annotations

import contextlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.schedule import store
from axiom.extensions.builtins.schedule.db_models import Base


@pytest.fixture
def sqlite_store():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, future=True)

    @contextlib.contextmanager
    def provider():
        session = maker()
        try:
            yield session
        finally:
            session.close()

    store.set_provider(provider)
    try:
        yield
    finally:
        store.reset_provider()
        engine.dispose()


@pytest.fixture(autouse=True)
def _clear_hooks():
    """Hooks are process-global; clear around every test so they don't leak."""
    from axiom.extensions.builtins.schedule import hooks

    hooks.clear()
    yield
    hooks.clear()
