# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``fork`` copies a conversation under a new id without ever crossing an owner boundary."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.chat.db_models import Base
from axiom.infra.orchestrator.session import Session, SessionStore
from axiom.infra.orchestrator.session_db import DatabaseSessionStore


def _spoken(store, principal="@alice:s", *, site="", tenant="", title="plan"):
    s = Session(principal_id=principal, site_id=site, tenant_id=tenant, title=title)
    s.context = {"nested": {"k": [1, 2]}}
    s.add_message("user", "one")
    s.add_message("assistant", "two")
    s.add_message("user", "three")
    store.save(s)
    return s


@pytest.fixture(params=["file", "db"])
def store(request, tmp_path):
    if request.param == "file":
        yield SessionStore(sessions_dir=tmp_path)
        return
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(engine)
    try:
        yield DatabaseSessionStore(engine=engine)
    finally:
        engine.dispose()


def test_a_fork_is_a_new_session_with_the_same_messages(store):
    src = _spoken(store)
    fork = store.fork(src.session_id, principal="@alice:s")
    assert fork is not None and fork.session_id != src.session_id
    assert [(m.role, m.content) for m in fork.messages] == [(m.role, m.content) for m in src.messages]
    assert store.load(fork.session_id).session_id == fork.session_id  # persisted


def test_the_original_is_untouched_and_the_copies_are_independent(store):
    src = _spoken(store)
    fork = store.fork(src.session_id, principal="@alice:s")
    fork.add_message("user", "only in the fork")
    fork.context["nested"]["k"].append(99)
    store.save(fork)
    again = store.load(src.session_id)
    assert len(again.messages) == 3
    assert again.context["nested"]["k"] == [1, 2]
    assert "forked_from" not in again.context


def test_title_gets_the_fork_suffix_and_an_explicit_title_wins(store):
    src = _spoken(store, title="plan")
    assert store.fork(src.session_id, principal="@alice:s").title == "plan (fork)"
    assert store.fork(src.session_id, title="try B", principal="@alice:s").title == "try B (fork)"


def test_forked_from_records_the_source_and_the_message_index(store):
    src = _spoken(store)
    fork = store.fork(src.session_id, principal="@alice:s")
    assert fork.context["forked_from"]["session_id"] == src.session_id
    assert fork.context["forked_from"]["message_index"] == 3
    assert store.load(fork.session_id).context["forked_from"]["message_index"] == 3


def test_fork_at_an_earlier_message_copies_only_the_prefix(store):
    src = _spoken(store)
    fork = store.fork(src.session_id, at=2, principal="@alice:s")
    assert [m.content for m in fork.messages] == ["one", "two"]
    assert fork.context["forked_from"]["message_index"] == 2


def test_a_fork_cannot_be_created_across_principals(store):
    src = _spoken(store, "@alice:s")
    assert store.fork(src.session_id, principal="@mallory:s") is None
    # and nothing was left behind
    assert [s for s in store.list_sessions() if s != src.session_id] == []


def test_owner_and_scope_axes_carry_to_the_fork(store):
    src = _spoken(store, "@alice:s", site="s1", tenant="t1")
    fork = store.fork(src.session_id, principal="@alice:s")
    loaded = store.load(fork.session_id)
    assert (loaded.principal_id, loaded.site_id, loaded.tenant_id) == ("@alice:s", "s1", "t1")


def test_forking_a_missing_session_is_none(store):
    assert store.fork("nope", principal="@alice:s") is None


def test_an_out_of_range_index_is_an_error_not_a_silent_clamp(store):
    src = _spoken(store)
    with pytest.raises(ValueError):
        store.fork(src.session_id, at=99, principal="@alice:s")
