# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chaos: one chat, many surfaces — does a surface keep its users apart?

The promise is that the same assistant answers on every surface with the same
retrieval, tools and governance. The failure that promise invites is not "the
surfaces disagree" but "the surfaces agree TOO much" — sharing state they
should not.

What this found, before the fix:

- every HTTP caller received the SAME `ChatAgent` holding the SAME `Session`,
  so one person's turns were in the next person's history, and history is what
  goes into the next prompt;
- identity was assigned per request onto that shared object, so two concurrent
  requests raced and a turn could run under the wrong person's identity — two
  threads, both observing `@bob`;
- `/chat/reset` dropped the process-wide agent, so any caller could wipe every
  other caller's conversation.
"""

from __future__ import annotations

import threading

import pytest

from axiom.extensions.builtins.http import chat_server

ALICE = {"principal": "@alice:example", "name": "Alice"}
BOB = {"principal": "@bob:example", "name": "Bob"}


@pytest.fixture(autouse=True)
def _clear_registry():
    with chat_server._agent_lock:
        chat_server._agents.clear()
    yield
    with chat_server._agent_lock:
        chat_server._agents.clear()


class TestUsersAreKeptApart:
    def test_two_identities_never_share_a_session(self):
        alice = chat_server._get_agent(ALICE)
        bob = chat_server._get_agent(BOB)
        assert alice is not bob
        assert alice.session is not bob.session

    def test_one_identity_keeps_its_conversation(self):
        """Isolation must not cost continuity — the same person coming back
        has to find their own history."""
        first = chat_server._get_agent(ALICE)
        second = chat_server._get_agent(ALICE)
        assert first is second

    def test_unidentified_callers_never_share_a_session(self):
        """No identity means NO memory, not SHARED memory. Two strangers
        must not land in one conversation just because neither said who
        they were."""
        first = chat_server._get_agent(None)
        second = chat_server._get_agent({})
        assert first is not second
        assert first.session is not second.session

    def test_identity_is_not_shared_mutable_state(self):
        """Each request now writes identity onto its OWN agent. Previously
        both threads observed the same value — whichever wrote last."""
        observed: dict[str, str] = {}
        barrier = threading.Barrier(2)

        def request(context: dict):
            agent = chat_server._get_agent(context)
            agent.session.context["user_identity"] = context
            barrier.wait(timeout=5)  # both have written; now both read
            observed[context["principal"]] = agent.session.context[
                "user_identity"
            ]["principal"]

        threads = [threading.Thread(target=request, args=(c,)) for c in (ALICE, BOB)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert observed == {
            "@alice:example": "@alice:example",
            "@bob:example": "@bob:example",
        }, f"identity bled between concurrent requests: {observed}"


class TestConcurrentArrival:
    def test_the_same_identity_arriving_at_once_gets_one_agent(self):
        """Two tabs, one person, same instant. If the double-checked build
        hands back different agents, one tab's turns vanish from the other's
        history — and whichever agent loses the race takes its Session with it.
        """
        agents = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def arrive():
            barrier.wait(timeout=5)
            agent = chat_server._get_agent(ALICE)
            with lock:
                agents.append(agent)

        threads = [threading.Thread(target=arrive) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(agents) == 8
        # Assert on the SET, never on which one won — a concurrency test that
        # asserts on order tests the scheduler, not the code.
        assert len({id(a) for a in agents}) == 1, "the same person got several sessions"

    def test_the_registry_is_bounded(self):
        """The surface takes identities from its clients, so an unbounded
        per-caller cache is a memory-exhaustion vector."""
        for n in range(chat_server.MAX_CACHED_AGENTS + 20):
            chat_server._get_agent({"principal": f"@user{n}:example"})
        assert len(chat_server._agents) <= chat_server.MAX_CACHED_AGENTS

    def test_eviction_drops_the_least_recently_used(self):
        chat_server._get_agent(ALICE)
        for n in range(chat_server.MAX_CACHED_AGENTS):
            chat_server._get_agent({"principal": f"@filler{n}:example"})
            chat_server._get_agent(ALICE)  # keep Alice warm
        assert chat_server.agent_key(ALICE) in chat_server._agents


class TestResetIsScopedToTheCaller:
    def test_one_caller_cannot_wipe_another(self):
        alice = chat_server._get_agent(ALICE)
        bob = chat_server._get_agent(BOB)
        chat_server.reset_agent(BOB)
        assert chat_server._get_agent(ALICE) is alice, (
            "one caller's reset destroyed another caller's conversation"
        )
        assert chat_server._get_agent(BOB) is not bob

    def test_an_unidentified_caller_resets_nothing(self):
        alice = chat_server._get_agent(ALICE)
        assert chat_server.reset_agent(None) is False
        assert chat_server._get_agent(ALICE) is alice
