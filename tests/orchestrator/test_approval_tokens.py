# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Answering an approval from email or SMS, and only from there."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.infra.orchestrator.actions import ActionStatus, create_action
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import FileActionStore
from axiom.infra.orchestrator.approval_tokens import (
    CODE_ALPHABET,
    MAX_ATTEMPTS,
    REMOTE_CHANNELS,
    TokenStore,
    UnsupportedChannel,
    redeem_and_decide,
)


@pytest.fixture
def store(tmp_path):
    return TokenStore(tmp_path / "tokens.json")


@pytest.fixture
def gate(tmp_path):
    return ApprovalGate(FileActionStore(tmp_path / "approvals.json"))


def _held(gate):
    return gate.submit(create_action("doc_publish", {"source": "prd_foo.md"}))


class TestOnlyChannelsTheAgentCannotRead:
    """The rule that decides whether a token means anything.

    A nonce controls a party that cannot observe the channel it arrived on.
    The agent has no mailbox and no handset; it does have the transcript.
    """

    @pytest.mark.parametrize("channel", sorted(REMOTE_CHANNELS))
    def test_email_and_sms_get_one(self, store, channel):
        assert store.mint("a1", channel=channel)

    @pytest.mark.parametrize("channel", ["chat", "cli", "mcp", "slack", "teams"])
    def test_everything_else_is_refused(self, store, channel):
        with pytest.raises(UnsupportedChannel, match="cannot observe"):
            store.mint("a1", channel=channel)

    def test_the_refusal_is_loud_rather_than_a_none(self, store):
        """A caller handed None would most likely send the approval anyway,
        with no control at all, believing there was one."""
        with pytest.raises(UnsupportedChannel):
            store.mint("a1", channel="chat")


class TestTheSecretIsNotWrittenDown:
    def test_the_plaintext_never_reaches_the_file(self, store, tmp_path):
        secret = store.mint("a1", channel="email")
        assert secret not in (tmp_path / "tokens.json").read_text()

    def test_an_agent_reading_the_queue_cannot_find_a_token(self, store, gate, tmp_path):
        """``approval.pending`` is agent-reachable on purpose.

        A token living on the Action would be a token the agent could read out
        of its own queue, which is the one party it exists to exclude.
        """
        action = _held(gate)
        secret = store.mint(action.action_id, channel="sms")

        queue = (tmp_path / "approvals.json").read_text()
        assert secret not in queue
        assert "token" not in queue.lower()

    def test_it_is_still_redeemable(self, store):
        """Hashing at rest must not cost the feature."""
        secret = store.mint("a1", channel="email")
        assert store.redeem(secret) == "a1"


class TestSpendingIt:
    def test_a_token_works_once(self, store):
        secret = store.mint("a1", channel="email")

        assert store.redeem(secret) == "a1"
        assert store.redeem(secret) is None, "single use"

    def test_a_wrong_secret_answers_nothing(self, store):
        store.mint("a1", channel="email")
        assert store.redeem("not-the-token") is None

    def test_every_failure_looks_the_same(self, store, tmp_path):
        """Telling a guesser why a code failed tells them whether they are close."""
        expired = TokenStore(
            tmp_path / "t.json", now=datetime.now(UTC) - timedelta(days=30)
        )
        stale = expired.mint("a1", channel="sms")
        fresh = TokenStore(tmp_path / "t.json")

        assert fresh.redeem(stale) is None
        assert fresh.redeem("never-existed") is None

    def test_an_expired_token_is_refused(self, tmp_path):
        old = TokenStore(tmp_path / "t.json", ttl_hours=1)
        secret = old.mint("a1", channel="sms")

        later = TokenStore(
            tmp_path / "t.json", now=datetime.now(UTC) + timedelta(hours=2)
        )
        assert later.redeem(secret) is None

    def test_guessing_is_bounded(self, store):
        """An eight-character code is guessable if you may keep guessing."""
        secret = store.mint("a1", channel="sms")
        for _ in range(MAX_ATTEMPTS):
            store.record_failure("a1")

        assert store.redeem(secret) is None
        assert store.live_for("a1") == []


class TestItSurvivesTheProcess:
    def test_minted_in_one_process_redeemed_in_another(self, tmp_path):
        secret = TokenStore(tmp_path / "t.json").mint("a1", channel="email")

        assert TokenStore(tmp_path / "t.json").redeem(secret) == "a1"

    def test_the_whole_round_trip(self, tmp_path, gate, store):
        """What an email handler actually does, end to end."""
        action = _held(gate)
        secret = store.mint(action.action_id, channel="email")

        settled = redeem_and_decide(
            secret, gate, approving=True, decided_by="@ben:ut", store=store
        )

        assert settled.status == ActionStatus.APPROVED
        assert settled.decided_by == "@ben:ut"

    def test_a_rejection_by_token_carries_the_reason(self, gate, store):
        action = _held(gate)
        secret = store.mint(action.action_id, channel="sms")

        settled = redeem_and_decide(
            secret,
            gate,
            approving=False,
            decided_by="@ben:ut",
            store=store,
            reason="wrong document",
        )

        assert settled.status == ActionStatus.REJECTED
        assert settled.error == "wrong document"

    def test_a_bad_token_settles_nothing(self, gate, store):
        action = _held(gate)

        assert (
            redeem_and_decide(
                "wrong", gate, approving=True, decided_by="@ben:ut", store=store
            )
            is None
        )
        assert gate.get(action.action_id).status == ActionStatus.PENDING

    def test_the_token_is_spent_even_though_it_answers_once(self, gate, store):
        """A replayed link must not approve a second action later."""
        action = _held(gate)
        secret = store.mint(action.action_id, channel="email")
        redeem_and_decide(secret, gate, approving=True, decided_by="@ben:ut", store=store)

        assert store.redeem(secret) is None


class TestCodesAreTypeable:
    def test_the_alphabet_excludes_every_character_it_means_to(self):
        """Asserted on the ALPHABET, not on a sample.

        The first version drew a code and checked that. An 8 was in the
        alphabet, so the test failed only when an 8 happened to be drawn —
        roughly one run in four, and it passed the run before the one that
        caught it. A property of a generator has to be tested on the
        generator; sampling turns a certainty into a coin flip.
        """
        from axiom.infra.orchestrator.approval_tokens import AMBIGUOUS

        assert not set(CODE_ALPHABET) & set(AMBIGUOUS)

    def test_a_minted_code_uses_only_that_alphabet(self, store):
        code = store.mint("a1", channel="sms")

        assert set(code) <= set(CODE_ALPHABET)
        assert len(code) == 8

    def test_a_link_token_is_long_because_nobody_types_it(self, store):
        assert len(store.mint("a1", channel="email")) > 30


class TestHousekeeping:
    def test_spent_and_expired_tokens_are_droppable(self, tmp_path):
        store = TokenStore(tmp_path / "t.json")
        used = store.mint("a1", channel="email")
        store.mint("a2", channel="email")
        store.redeem(used)

        assert store.purge_spent() == 1
        assert len(store.live_for("a2")) == 1
