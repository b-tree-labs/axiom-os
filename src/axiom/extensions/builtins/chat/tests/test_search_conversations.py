# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A conversation is found by what was SAID in it, not only by its title.

The list endpoint's ``search`` matched titles only, so the app's search found
nothing for "control rod" in a conversation titled "Morning check" that talked
about control rods for twenty messages. ``search_conversations`` matches
message text too, with the same site and ownership bounds as the listing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.chat.db_models import Base
from axiom.infra.orchestrator.session_db import DatabaseSessionStore, _snippet


@pytest.fixture
def store():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(engine)
    try:
        yield DatabaseSessionStore(engine=engine)
    finally:
        engine.dispose()


def _conv(store, *messages, site="s1", principal=""):
    s = store.create(site_id=site, tenant_id="", principal_id=principal)
    for m in messages:
        s.add_message("user", m)
    store.save(s)
    return s


def test_a_match_in_the_messages_is_found(store):
    s = _conv(store, "Morning check", "the control rod drive reads oddly today")
    (hit,) = store.search_conversations("control rod")
    assert hit["id"] == s.session_id
    assert hit["matched"] in {"message", "title"}
    if hit["matched"] == "message":
        assert "control rod" in hit["snippet"]


def test_nothing_matches_nothing(store):
    _conv(store, "hello there")
    assert store.search_conversations("control rod") == []
    assert store.search_conversations("   ") == []


def test_another_principals_conversation_is_not_found(store):
    _conv(store, "control rod notes", principal="@ada:local")
    mine = _conv(store, "control rod for me", principal="@ben:local")
    ids = {h["id"] for h in store.search_conversations("control rod", principal="@ben:local")}
    assert ids == {mine.session_id}


def test_site_scope_bounds_the_search(store):
    here = _conv(store, "control rod", site="s1")
    _conv(store, "control rod", site="s2")
    ids = {h["id"] for h in store.search_conversations("control rod", site_id="s1")}
    assert ids == {here.session_id}


def test_the_snippet_is_a_window_around_the_match():
    text = "x " * 200 + "the CONTROL ROD drive" + " y" * 200
    snip = _snippet(text, "control rod", width=60)
    assert "CONTROL ROD" in snip
    assert snip.startswith("…") and snip.endswith("…")
    assert len(snip) <= 62
