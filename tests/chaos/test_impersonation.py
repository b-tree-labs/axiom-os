# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Security: a caller cannot become someone else by saying so.

The served surface filed each caller's conversation under an identity read out
of the REQUEST BODY — `user_context`, which the caller writes. Nothing checked
it against the credential the caller presented.

So the isolation the registry provides was decoration: it isolated on a key
the attacker chose. In a deployment where everyone is authenticated — which is
what a second surface is FOR — any user could have asked for any other user's
conversation by naming them.

The rule now: a VERIFIED principal, resolved from the credential by the authz
layer, always wins. A claimed identity is still honoured where no credential
resolves to a person, because a shared API key would otherwise put a whole
team in one conversation — but it is namespaced, so a claim can never collide
with a verified identity however it is spelled.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.http import chat_server
from axiom.extensions.builtins.http.chat_server import (
    UNVERIFIED_PREFIX,
    agent_key,
    openai_user_context,
)

ALICE_VERIFIED = "@alice:example"


@pytest.fixture(autouse=True)
def _clear_registry():
    with chat_server._agent_lock:
        chat_server._agents.clear()
    yield
    with chat_server._agent_lock:
        chat_server._agents.clear()


class TestAClaimCannotReachAVerifiedIdentity:
    def test_claiming_someone_elses_handle_does_not_reach_them(self):
        """The attack, directly: Alice is verified; Mallory names Alice in the
        body and must not land in Alice's conversation."""
        alice = chat_server._get_agent(None, verified_principal=ALICE_VERIFIED)
        mallory = chat_server._get_agent({"principal": ALICE_VERIFIED})
        assert mallory is not alice
        assert mallory.session is not alice.session

    def test_a_claimed_key_is_never_a_verified_key(self):
        assert agent_key({"principal": ALICE_VERIFIED}) != agent_key(
            None, verified_principal=ALICE_VERIFIED
        )

    @pytest.mark.parametrize(
        "spelling",
        [
            "@alice:example",
            "@ALICE:EXAMPLE",
            "  @alice:example  ",
            "unverified:@alice:example",
            "unverified:unverified:@alice:example",
        ],
    )
    def test_no_spelling_of_a_claim_collides_with_the_verified_key(self, spelling):
        """Including a claim that tries to spell the namespace itself — the
        prefix is applied to whatever was claimed, so claiming the prefix just
        buries it one level deeper."""
        assert agent_key({"principal": spelling}) != ALICE_VERIFIED

    def test_the_prefix_is_not_a_valid_handle(self):
        """Which is why a claim can never equal a verified handle: verified
        handles are ADR-020 handles, and this is not one."""
        assert UNVERIFIED_PREFIX.endswith(":")
        assert not UNVERIFIED_PREFIX.startswith("@")

    def test_the_verified_principal_beats_a_conflicting_claim(self):
        """A caller whose credential says Alice, claiming to be Bob, is Alice."""
        key = agent_key({"principal": "@bob:example"}, verified_principal=ALICE_VERIFIED)
        assert key == ALICE_VERIFIED


class TestClaimsStillWorkWhereNothingBetterExists:
    """A shared API key identifies a client, not a person. Refusing claims
    outright would put a whole team in one conversation, which is the failure
    the registry exists to prevent."""

    def test_two_claimants_get_two_conversations(self):
        first = chat_server._get_agent({"principal": "alice"})
        second = chat_server._get_agent({"principal": "bob"})
        assert first is not second

    def test_the_same_claimant_keeps_its_conversation(self):
        first = chat_server._get_agent({"principal": "alice"})
        second = chat_server._get_agent({"principal": "alice"})
        assert first is second

    def test_an_openai_client_claim_is_also_namespaced(self):
        key = agent_key(openai_user_context({"user": "alice"}))
        assert key.startswith(UNVERIFIED_PREFIX)


class TestResetCannotBeAimedAtSomeoneElse:
    def test_a_claim_cannot_reset_a_verified_callers_session(self):
        alice = chat_server._get_agent(None, verified_principal=ALICE_VERIFIED)
        assert chat_server.reset_agent({"principal": ALICE_VERIFIED}) is False
        assert (
            chat_server._get_agent(None, verified_principal=ALICE_VERIFIED) is alice
        ), "a claimed identity destroyed a verified caller's conversation"


class TestTheVerifiedPrincipalComesFromAnAllowedDecision:
    def test_it_is_read_from_request_state(self):
        from types import SimpleNamespace

        request = SimpleNamespace(state=SimpleNamespace(principal="@alice:example"))
        assert chat_server.verified_principal_of(request) == "@alice:example"

    def test_a_request_without_one_yields_nothing(self):
        from types import SimpleNamespace

        assert chat_server.verified_principal_of(SimpleNamespace()) == ""
        assert (
            chat_server.verified_principal_of(SimpleNamespace(state=SimpleNamespace()))
            == ""
        )

    def test_a_non_string_principal_is_refused(self):
        """Defensive: the value becomes a cache key, so anything unexpected
        must yield NO identity rather than a wrong one."""
        from types import SimpleNamespace

        request = SimpleNamespace(state=SimpleNamespace(principal={"not": "a handle"}))
        assert chat_server.verified_principal_of(request) == ""
