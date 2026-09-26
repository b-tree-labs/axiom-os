# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Store-backend selection + local→Site reconcile (C2 local-dev + onboarding).

The web surface uses one store API over either engine; selection is honest
(injected > configured database > zero-config local SQLite), and joining a Site
moves local history into the Site's store idempotently, preserving ids + scope.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.chat.db_models import Base
from axiom.infra.orchestrator import conversation_store as cs
from axiom.infra.orchestrator.session_db import DatabaseSessionStore


def _sqlite_store():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(engine)
    return DatabaseSessionStore(engine=engine), engine


def _seed(store, *, title, site="", tenant="", principal="", starred=False, msgs=("hi",)):
    s = store.create(site_id=site, tenant_id=tenant, principal_id=principal)
    if title:
        store.rename(s.session_id, title)
    for m in msgs:
        s.add_message("user", m)
    if s.messages:
        store.save(s)
    if starred:
        store.set_starred(s.session_id, True)
    return s.session_id


# ---- selection -----------------------------------------------------------

def test_injected_engine_wins(monkeypatch):
    _, engine = _sqlite_store()
    store = cs.resolve_conversation_store(engine=engine)
    assert isinstance(store, DatabaseSessionStore)
    # usable
    s = store.create(site_id="s1", tenant_id="t1")
    assert store.conversation(s.session_id) is not None


def test_no_database_env_uses_local_sqlite(monkeypatch):
    monkeypatch.delenv("AXIOM_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _, engine = _sqlite_store()
    monkeypatch.setattr(cs, "local_sqlite_engine", lambda path=None: engine)
    store = cs.resolve_conversation_store()
    assert isinstance(store, DatabaseSessionStore)
    s = store.create(site_id="", tenant_id="")  # works with nothing configured
    assert store.conversation(s.session_id) is not None


def test_database_env_uses_configured(monkeypatch):
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://u:p@db.internal:5432/axiom_db")
    store = cs.resolve_conversation_store()
    assert isinstance(store, DatabaseSessionStore)
    assert store._engine is None  # the session_for("chat") path, not a local engine


# ---- reconcile -----------------------------------------------------------

def test_reconcile_moves_scopes_and_preserves(monkeypatch):
    local, _ = _sqlite_store()
    shared, _ = _sqlite_store()
    a = _seed(local, title="alpha", starred=True, msgs=("q1", "q2"))
    b = _seed(local, title="beta", msgs=("only",))
    _seed(local, title="empty", msgs=())  # empty → skipped

    migrated = cs.reconcile_local_to_shared(local, shared, principal="@alice:example", site_id="site-a", tenant_id="tenant-a")
    assert migrated == 2

    metas = {m["id"]: m for m in shared.list_meta(site_id="site-a")}
    assert set(metas) == {a, b}  # same ids preserved
    assert metas[a]["tenant_id"] == "tenant-a" and metas[a]["site_id"] == "site-a"
    assert metas[a]["starred"] is True  # star carried
    detail = shared.get_detail(a, principal="@alice:example")
    assert [m["content"] for m in detail["messages"]] == ["q1", "q2"]  # messages carried, ordered

    # Idempotent: a second run migrates nothing new and does not duplicate.
    assert cs.reconcile_local_to_shared(local, shared, principal="@alice:example", site_id="site-a", tenant_id="tenant-a") == 0
    assert len(shared.list_meta(site_id="site-a")) == 2
