# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Site/tenant scoping + starred on the DB session store (C2 step 1).

The hosted store was principal-scoped only — the KEY GAP for a multi-tenant
web surface, where reads must be scoped to a site (physical install) and a
tenant (the account the web filters by, appkit's ``account_id``). These tests
pin the added axes and the ``list_meta`` projection the /api/v1/chat router
lists from, using the engine-injection seam with in-memory SQLite.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.chat.db_models import Base
from axiom.infra.orchestrator.session_db import DatabaseSessionStore


@pytest.fixture
def store():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    try:
        yield DatabaseSessionStore(engine=engine)
    finally:
        engine.dispose()


def _mk(store, *, site="", tenant="", principal="", title="hi"):
    s = store.create(site_id=site, tenant_id=tenant, principal_id=principal)
    s.add_message("user", title)
    store.save(s)
    return s


def test_list_meta_scoped_by_site_and_tenant(store):
    a = _mk(store, site="s1", tenant="t1", title="alpha")
    b = _mk(store, site="s1", tenant="t2", title="beta")
    _mk(store, site="s2", tenant="t1", title="gamma")
    assert {m["id"] for m in store.list_meta(site_id="s1")} == {a.session_id, b.session_id}
    assert {m["id"] for m in store.list_meta(site_id="s1", tenant_id="t1")} == {a.session_id}
    assert len(store.list_meta()) == 3  # unscoped sees all


def test_list_meta_returns_metadata(store):
    s = _mk(store, site="s1", tenant="t1", title="hello world")
    (meta,) = store.list_meta(site_id="s1")
    assert meta["id"] == s.session_id
    assert meta["title"] == "hello world"
    assert meta["starred"] is False
    assert meta["tenant_id"] == "t1"
    assert meta["site_id"] == "s1"
    assert meta["updated_at"]


def test_starred_toggle_and_preserved_across_saves(store):
    s = _mk(store, site="s1", tenant="t1")
    assert store.set_starred(s.session_id, True) is True
    (meta,) = store.list_meta(site_id="s1")
    assert meta["starred"] is True
    # An append-save must NOT clobber the column-set star.
    reloaded = store.load(s.session_id)
    reloaded.add_message("assistant", "ok")
    store.save(reloaded)
    (meta,) = store.list_meta(site_id="s1")
    assert meta["starred"] is True


def test_move_scope(store):
    s = _mk(store, site="s1", tenant="t1")
    assert store.set_scope(s.session_id, tenant_id="t2") is True
    assert {m["id"] for m in store.list_meta(site_id="s1", tenant_id="t2")} == {s.session_id}
    assert store.list_meta(site_id="s1", tenant_id="t1") == []


def test_load_populates_scope(store):
    s = _mk(store, site="s1", tenant="t1", principal="@p:x")
    loaded = store.load(s.session_id)
    assert loaded is not None
    assert loaded.site_id == "s1"
    assert loaded.tenant_id == "t1"


def test_search_filters_title(store):
    _mk(store, site="s1", tenant="t1", title="soil report")
    _mk(store, site="s1", tenant="t1", title="weather forecast")
    hits = [m["title"] for m in store.list_meta(site_id="s1", search="soil")]
    assert hits == ["soil report"]


def test_principal_isolation_same_site(store):
    """A conversation belongs to a PERSON, not just a site — principal and site
    are ANDed. On a single-site node, one principal must not see another's chats.
    (This fails if the site filter ever REPLACES the principal filter; the site
    tests pass either way, so this is the guard for it.)"""
    a = _mk(store, site="s1", tenant="t1", principal="@a:x", title="secret A")
    b = _mk(store, site="s1", tenant="t1", principal="@b:y", title="secret B")
    # A lists only A's, even scoped to the shared site.
    assert {m["id"] for m in store.list_meta(site_id="s1", principal="@a:x")} == {a.session_id}
    assert {m["id"] for m in store.list_meta(site_id="s1", principal="@b:y")} == {b.session_id}
    # A cannot read B's, by either reader.
    assert store.conversation(b.session_id, principal="@a:x") is None
    assert store.get_detail(b.session_id, principal="@a:x") is None
    # and B cannot read A's.
    assert store.conversation(a.session_id, principal="@b:y") is None


def test_archived_excluded_by_default(store):
    s = _mk(store, site="s1", tenant="t1")
    store.archive(s.session_id)
    assert store.list_meta(site_id="s1") == []
    assert len(store.list_meta(site_id="s1", include_archived=True)) == 1


# --- the page limit must bound READABLE rows, not raw rows ------------------


def test_limit_pages_the_readable_set_not_the_raw_table(store):
    """A limit applied before the ownership filter pages the wrong set.

    On a shared install another principal's conversations can fill the SQL page
    entirely, and every one is then dropped in Python — so the asker's list
    comes back short, or empty, while their conversations plainly exist. The
    limit has to bound what the caller may actually see.
    """
    for _ in range(5):
        _mk(store, site="s", tenant="t", principal="@mine:local")
    for _ in range(5):  # created later, so these sort FIRST
        _mk(store, site="s", tenant="t", principal="@other:local")

    assert len(store.list_meta(site_id="s", tenant_id="t", principal="@mine:local")) == 5
    paged = store.list_meta(site_id="s", tenant_id="t", principal="@mine:local", limit=5)
    assert len(paged) == 5, f"limit paged the raw table, not the readable set: got {len(paged)}"


def test_negative_control_a_limit_still_actually_limits(store):
    # Proves the fix did not simply stop honouring the limit.
    for _ in range(5):
        _mk(store, site="s", tenant="t", principal="@mine:local")
    assert len(store.list_meta(site_id="s", tenant_id="t", principal="@mine:local", limit=2)) == 2


def test_another_principals_rows_are_still_never_returned(store):
    # The fix must not widen what a principal can read.
    _mk(store, site="s", tenant="t", principal="@other:local")
    assert store.list_meta(site_id="s", tenant_id="t", principal="@mine:local") == []
