# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chaos: many surfaces means many CLIENTS, each with its own protocol.

The surfaces are not variations on one client. They are different protocols
with different identity conventions, and the promise only holds if a person is
the same person across all of them:

| client                          | protocol            | identity arrives as |
|---------------------------------|---------------------|---------------------|
| the CLI / TUI                   | in-process          | node principal      |
| the site's own web chat         | `POST /chat`        | `user_context`      |
| OpenWebUI, LibreChat, SDKs      | `/v1/chat/completions` | `user` field     |
| a scripted API client           | `/v1/chat/completions` | bearer token     |
| a polling client's model picker | `/v1/models`        | none needed         |

The first pass at per-caller isolation keyed only on `user_context`, which is
OUR field. Every OpenAI-protocol client therefore became unidentified — and
since an unidentified caller deliberately gets no continuity, those clients
silently lost their history between turns. Isolation must not be bought with
amnesia on exactly the clients a second surface is for.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.http import chat_server
from axiom.extensions.builtins.http.chat_server import (
    agent_key,
    openai_user_context,
)


@pytest.fixture(autouse=True)
def _clear_registry():
    with chat_server._agent_lock:
        chat_server._agents.clear()
    yield
    with chat_server._agent_lock:
        chat_server._agents.clear()


class TestEveryClientProtocolCarriesIdentity:
    """If a protocol cannot express who is calling, that client gets no
    continuity — so each one we support must be able to say."""

    def test_the_sites_own_client_identifies_by_user_context(self):
        """Namespaced, because the body is caller-written. See
        tests/chaos/test_impersonation.py for why."""
        assert agent_key({"principal": "@alice:example"}) == (
            "unverified:@alice:example"
        )

    def test_a_verified_principal_is_used_unnamespaced(self):
        assert agent_key(None, verified_principal="@alice:example") == (
            "@alice:example"
        )

    def test_openai_clients_identify_by_the_standard_user_field(self):
        """OpenWebUI and LibreChat both send `user`; it is in the OpenAI
        request schema precisely for this."""
        context = openai_user_context({"user": "alice@example.edu"})
        assert agent_key(context) == "unverified:alice@example.edu"

    def test_a_scripted_client_identifies_by_its_bearer_credential(self):
        context = openai_user_context({}, "Bearer sk-test-abc123")
        assert agent_key(context).startswith("unverified:@client-")

    def test_a_credential_is_never_used_as_the_cache_key(self):
        """A cache key is not a place to keep a secret."""
        token = "sk-super-secret-value"
        key = agent_key(openai_user_context({}, f"Bearer {token}"))
        assert token not in key

    def test_two_bearer_tokens_are_two_callers(self):
        first = agent_key(openai_user_context({}, "Bearer sk-one"))
        second = agent_key(openai_user_context({}, "Bearer sk-two"))
        assert first != second

    def test_the_same_bearer_token_is_the_same_caller(self):
        """Digests must be stable, or a scripted client loses its history on
        every request."""
        first = agent_key(openai_user_context({}, "Bearer sk-same"))
        second = agent_key(openai_user_context({}, "Bearer sk-same"))
        assert first == second

    def test_the_body_field_wins_over_the_credential(self):
        """A shared API key with per-user `user` fields is the normal
        deployment for a team behind OpenWebUI. Keying on the key alone would
        put the whole team in one conversation."""
        context = openai_user_context({"user": "alice"}, "Bearer shared-team-key")
        assert agent_key(context) == "unverified:alice"

    def test_an_anonymous_client_is_identified_as_nobody(self):
        assert openai_user_context({}, "") is None
        assert agent_key(None) == ""


class TestClientsDoNotCollide:
    def test_a_team_behind_one_api_key_gets_one_session_each(self):
        alice = chat_server._get_agent(openai_user_context({"user": "alice"}, "Bearer k"))
        bob = chat_server._get_agent(openai_user_context({"user": "bob"}, "Bearer k"))
        assert alice is not bob

    def test_a_person_is_the_same_person_across_two_clients(self):
        """The whole promise: the site's web chat and an OpenAI client, same
        principal, same conversation."""
        from_web = chat_server._get_agent({"principal": "alice@example.edu"})
        from_openai = chat_server._get_agent(
            openai_user_context({"user": "alice@example.edu"})
        )
        assert from_web is from_openai, (
            "the same person on two clients landed in two conversations"
        )

    def test_identity_is_matched_case_insensitively(self):
        """One client sends the address as typed, another lower-cases it."""
        first = chat_server._get_agent({"principal": "Alice@Example.edu"})
        second = chat_server._get_agent({"principal": "alice@example.edu"})
        assert first is second


class TestPollingClientsAreCheap:
    def test_the_model_list_does_not_build_a_conversation(self):
        """OpenWebUI polls /v1/models on a timer. Building an agent and a
        Session to read one string made every poll pay for a conversation it
        immediately discarded."""
        chat_server.backend_model_name()
        assert chat_server._agents == {}, (
            "a metadata poll created a cached conversation"
        )
