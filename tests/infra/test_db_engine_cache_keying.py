# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The engine cache must follow ``AXIOM_DB_URL``, not merely be non-None.

The cache was a bare ``if _engine is None``, so the first caller in a process
fixed the database for every caller after it. A test that pointed the URL
somewhere else — deliberately or by repointing HOME — left a stale Engine
behind, and everything downstream silently used the wrong database while
looking healthy. Keying the cache on the URL makes a changed URL rebuild.
"""

from __future__ import annotations

import axiom.infra.db as db


def _reset() -> None:
    db._engine = None
    db._session_factory = None
    db._engine_url = None


def test_same_url_returns_the_same_engine(monkeypatch):
    _reset()
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql+psycopg2://a:b@127.0.0.1:5432/one")
    first = db.get_engine()
    assert db.get_engine() is first, "same URL must reuse the pooled Engine"


def test_changed_url_rebuilds_the_engine(monkeypatch):
    _reset()
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql+psycopg2://a:b@127.0.0.1:5432/one")
    first = db.get_engine()
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql+psycopg2://a:b@127.0.0.1:5432/two")
    second = db.get_engine()
    assert second is not first, "a changed AXIOM_DB_URL must not return the stale Engine"
    assert "two" in str(second.url)


def test_session_factory_follows_the_rebuilt_engine(monkeypatch):
    _reset()
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql+psycopg2://a:b@127.0.0.1:5432/one")
    db.get_engine()
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql+psycopg2://a:b@127.0.0.1:5432/two")
    engine = db.get_engine()
    assert db._session_factory is not None
    assert db._session_factory.kw["bind"] is engine, "factory must bind the current Engine"


def test_unsetting_the_url_falls_back_and_rebuilds(monkeypatch):
    _reset()
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql+psycopg2://a:b@127.0.0.1:5432/one")
    first = db.get_engine()
    monkeypatch.delenv("AXIOM_DB_URL", raising=False)
    assert db.get_engine() is not first, "falling back to the default must also rebuild"
