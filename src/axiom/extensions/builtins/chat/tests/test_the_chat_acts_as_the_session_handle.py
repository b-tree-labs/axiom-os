# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The web chat acts as the session's ADR-020 handle, never the raw subject.

Measured on a node, 2026-11-10: the chat API used the session cookie's ``sub``
(an email) as the principal. The tool authority prefixed it to
``@name@domain``, the handle grammar rejected that, and with a site policy
loaded every tool call was denied fail-closed, so the chat could read no data.
Conversations filed under the raw subject must still be the person's own.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.chat import api
from axiom.extensions.builtins.chat.db_models import Base
from axiom.extensions.builtins.http.authz_hook import session_handle
from axiom.vega.identity.principal import parse_handle

SUB = "person@example.edu"


@pytest.fixture
def store(monkeypatch):
    monkeypatch.delenv("AXIOM_GATE_PRINCIPAL_CONTEXT", raising=False)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(engine)
    api.set_test_engine(engine)
    api._ADOPTED.clear()
    monkeypatch.setattr("axiom.webauth.session_from_cookies", lambda cookies: {"sub": SUB})
    try:
        yield api._store()
    finally:
        api.set_test_engine(None)
        engine.dispose()


def _request(principal=""):
    return SimpleNamespace(cookies={"webauth": "x"}, state=SimpleNamespace(principal=principal))


def test_the_principal_is_a_handle_the_authority_can_parse(store):
    handle = api._principal(_request())
    assert handle == session_handle(SUB) == "@person_example.edu:gate"
    parse_handle(handle)  # raises on the double-@ form


def test_the_verified_handle_wins(store):
    assert api._principal(_request("@someone:site")) == "@someone:site"


def test_history_under_the_raw_subject_follows_the_person(store):
    old = store.create(principal_id=SUB)
    old.add_message("user", "hello")
    store.save(old)
    handle = api._principal(_request())
    assert store.load(old.session_id, principal=handle) is not None
    assert store.conversation(old.session_id, principal=handle)["principal_id"] == handle


def test_reown_is_idempotent_and_ignores_empty_sides(store):
    s = store.create(principal_id=SUB)
    store.save(s)
    assert store.reown(SUB, "@p:gate") == 1
    assert store.reown(SUB, "@p:gate") == 0
    assert store.reown("", "@p:gate") == 0
    assert store.reown(SUB, SUB) == 0
