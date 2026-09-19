# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chaos: does a conversation exist outside the process that hosted it?

"One chat, many surfaces" cannot be true of a conversation only one process
can see. The served surface built a `Session` in memory and NEVER WROTE IT:
a conversation vanished on restart and was invisible to every other surface,
including the CLI running on the same machine against the same store.

Persisting it is the half that belongs here. Resuming a conversation started
elsewhere needs a principal-indexed lookup, which arrives with the database
session store — the tests for that live with it.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.http import chat_server
from axiom.infra.orchestrator.session import SessionStore

ALICE = {"principal": "@alice:example", "name": "Alice"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A store in a temp directory, standing in for the operator's real one.

    `persist_session` constructs `SessionStore()` itself, so the class it
    reaches for is what gets replaced — with a factory that ignores the
    default directory and returns this one.
    """
    real = SessionStore(tmp_path)
    monkeypatch.setattr(
        "axiom.infra.orchestrator.session.SessionStore", lambda *a, **k: real
    )
    return real


@pytest.fixture(autouse=True)
def _clear_registry():
    with chat_server._agent_lock:
        chat_server._agents.clear()
    yield
    with chat_server._agent_lock:
        chat_server._agents.clear()


class TestTheServedSurfacePersists:
    def test_a_turn_is_written_where_another_surface_can_find_it(self, store):
        agent = chat_server._get_agent(ALICE)
        agent.session.add_message("user", "does this outlive the process?")
        assert chat_server.persist_session(agent) is True

        recovered = store.load(agent.session.session_id)
        assert recovered is not None, "the served surface's conversation was lost"
        assert recovered.messages[-1].content == "does this outlive the process?"

    def test_a_persisted_session_says_whose_it_is(self):
        """Recorded ON the session, not only in the in-memory registry key —
        otherwise the conversation cannot be attributed once it leaves the
        process, and no other surface can find its owner."""
        agent = chat_server._get_agent(ALICE)
        assert agent.session.principal_id == "unverified:@alice:example"

    def test_an_anonymous_conversation_is_not_written(self, store):
        """Nobody could resume it, and unidentified traffic is exactly what an
        attacker controls — persisting it is unbounded disk growth on demand."""
        agent = chat_server._get_agent(None)
        agent.session.add_message("user", "anonymous turn")
        assert chat_server.persist_session(agent) is False


class TestPersistenceNeverCostsTheAnswer:
    def test_a_failing_store_does_not_fail_the_turn(self, monkeypatch):
        """The person already has their answer. Losing the transcript is not
        repaid by also losing the reply."""
        agent = chat_server._get_agent(ALICE)
        agent.session.add_message("user", "a turn")

        class Exploding:
            def __init__(self, *a, **k):
                raise OSError("disk full")

        monkeypatch.setattr(
            "axiom.infra.orchestrator.session.SessionStore", Exploding
        )
        assert chat_server.persist_session(agent) is False  # reported, not raised
