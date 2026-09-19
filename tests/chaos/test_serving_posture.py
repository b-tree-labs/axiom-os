# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Security: a surface answering strangers must not feed them each other.

The served surface ran on the DEFAULT `ChatScope`, where `index_transcript`
is True. After every turn the agent schedules an index of the session FILE
into the operator's corpus — the same corpus `_rag_context` retrieves from to
build the next prompt. One caller's conversation in another caller's prompt.

It was harmless for exactly one reason: the served surface never wrote a
session file, so the indexer found nothing. A protection made of an omission.
It stopped holding the moment this surface began persisting conversations —
three commits earlier on this same branch, by me, while fixing a different
problem.

That is why the posture is a named factory now rather than three fields a
second surface has to remember.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat.agent import _active_scope
from axiom.extensions.builtins.chat.scope import (
    OPERATOR_LOCAL_CORPUS,
    SHARED_CORPORA,
    serving_scope,
)
from axiom.extensions.builtins.http import chat_server

ALICE = {"principal": "@alice:example", "name": "Alice"}


@pytest.fixture(autouse=True)
def _clear_registry():
    with chat_server._agent_lock:
        chat_server._agents.clear()
    yield
    with chat_server._agent_lock:
        chat_server._agents.clear()


class TestTheServedSurfaceRunsUnderTheServingPosture:
    def test_a_served_conversation_is_never_indexed(self):
        """The write side. Indexing a stranger's conversation puts it where
        every later conversation retrieves from."""
        scope = _active_scope(chat_server._get_agent(ALICE), None)
        assert scope.index_transcript is False

    def test_a_served_prompt_never_retrieves_the_operators_transcripts(self):
        """The read side of the same disclosure. `None` means every corpus,
        including the operator-local one, so it must be declared."""
        scope = _active_scope(chat_server._get_agent(ALICE), None)
        assert scope.retrieval_corpora is not None
        assert OPERATOR_LOCAL_CORPUS not in scope.retrieval_corpora
        assert scope.retrieval_corpora == list(SHARED_CORPORA)

    def test_a_served_prompt_excludes_the_operators_own_files(self):
        scope = _active_scope(chat_server._get_agent(ALICE), None)
        assert scope.include_operator_local_prompts is False

    def test_the_scope_shares_the_agents_session(self):
        """The posture must not cost continuity: the scope's session is the
        one the surface persists, or turns would be written nowhere."""
        agent = chat_server._get_agent(ALICE)
        assert _active_scope(agent, None).session is agent.session

    def test_an_anonymous_caller_gets_the_posture_too(self):
        """Unidentified callers are still strangers."""
        scope = _active_scope(chat_server._get_agent(None), None)
        assert scope.index_transcript is False


class TestThePostureIsOneDefinition:
    """Three fields spread across each serving surface's constructor is how
    the HTTP surface came to miss all three. One factory, asserted here."""

    def test_the_factory_declares_all_three(self):
        scope = serving_scope()
        assert scope.index_transcript is False
        assert scope.retrieval_corpora == list(SHARED_CORPORA)
        assert scope.include_operator_local_prompts is False

    def test_the_default_scope_is_still_permissive(self):
        """Pinned deliberately. The operator's OWN terminal should index its
        transcripts — that is why they can ask next week about today. If this
        ever flips, the serving tests above would pass for the wrong reason
        and stop testing anything."""
        from axiom.extensions.builtins.chat.scope import ChatScope

        assert ChatScope().index_transcript is True

    def test_headless_and_http_agree(self):
        """Two serving surfaces, one posture. Drift between them is the
        original defect."""
        from axiom.extensions.builtins.chat.headless import HeadlessChat

        handle_scope = HeadlessChat(turn_deadline=30.0).new_scope()
        factory_scope = serving_scope()
        for field in (
            "index_transcript",
            "include_operator_local_prompts",
            "retrieval_corpora",
        ):
            assert getattr(handle_scope, field) == getattr(factory_scope, field), field


class TestTheApiKeyCheck:
    """Two faults on one small function."""

    def test_the_token_is_compared_in_constant_time(self):
        """A plain `==` returns as soon as two bytes differ, so the time it
        takes reveals how much of the token was right and it can be recovered
        a character at a time. Asserted on the SOURCE, because a timing
        difference this small is not measurable reliably in a test."""
        import inspect

        source = inspect.getsource(chat_server.NeutAPIHandler._check_auth)
        assert "compare_digest" in source
        assert "== self.api_key" not in source

    def test_an_unauthenticated_loopback_server_is_reported(self):
        message = chat_server.open_to_anyone("", "127.0.0.1")
        assert message
        assert "AXIOM_API_KEY" in message

    def test_an_unauthenticated_public_bind_is_reported_louder(self):
        """The same server on 0.0.0.0 answers the network, and the difference
        is one flag nobody re-reads."""
        message = chat_server.open_to_anyone("", "0.0.0.0")
        assert "SECURITY" in message
        assert "0.0.0.0" in message

    def test_a_configured_key_produces_no_warning(self):
        """Pinned so the warning cannot fire always and mean nothing."""
        assert chat_server.open_to_anyone("a-real-key", "0.0.0.0") == ""
        assert chat_server.open_to_anyone("a-real-key", "127.0.0.1") == ""
