# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The gate is the record, the channel is the doorbell."""

import pytest

from axiom.extensions.builtins.notifications.approval_bridge import (
    WITHHELD,
    bind_gate_to_channel,
    notify_pending,
    parse_option_id,
    pending_summary,
    summarize_for,
)
from axiom.extensions.builtins.notifications.channels.interactive import (
    InMemoryInteractiveChannel,
)
from axiom.governance import Classification
from axiom.infra.orchestrator.actions import ActionStatus, create_action
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import FileActionStore

INTERNAL = Classification.INTERNAL
CONTROLLED = Classification.CONTROLLED


def _gate(tmp_path):
    return ApprovalGate(FileActionStore(tmp_path / "approvals.json"))


def _held(gate):
    return gate.submit(create_action("doc_publish", {"source": "prd_foo.md"}))


class TestTheRoundTrip:
    def test_a_click_in_the_channel_settles_the_action_in_the_gate(self, tmp_path):
        gate = _gate(tmp_path)
        channel = InMemoryInteractiveChannel()
        bind_gate_to_channel(gate, channel)
        action = _held(gate)
        notify_pending(action, channel, classification=INTERNAL, channel_ceiling=INTERNAL)

        channel.inject_action(f"approve:{action.action_id}", actor="@ben:ut")

        settled = gate.get(action.action_id)
        assert settled.status == ActionStatus.APPROVED
        assert settled.decided_by == "@ben:ut", "the clicker is the decider"

    def test_rejection_carries_the_clicker_too(self, tmp_path):
        gate = _gate(tmp_path)
        channel = InMemoryInteractiveChannel()
        bind_gate_to_channel(gate, channel)
        action = _held(gate)

        channel.inject_action(f"reject:{action.action_id}", actor="@ben:ut")

        settled = gate.get(action.action_id)
        assert settled.status == ActionStatus.REJECTED
        assert settled.decided_by == "@ben:ut"

    def test_the_prompt_says_what_is_being_approved(self, tmp_path):
        """A prompt nobody can decide from gets approved reflexively."""
        gate = _gate(tmp_path)
        channel = InMemoryInteractiveChannel()
        notify_pending(_held(gate), channel, classification=INTERNAL, channel_ceiling=INTERNAL)

        posted = channel.posts[-1].text
        assert "doc_publish" in posted
        assert "prd_foo.md" in posted


class TestSharingAChannel:
    def test_a_click_from_another_conversation_is_left_alone(self, tmp_path):
        """One channel carries every conversation's buttons.

        An incident's Acknowledge is not malformed input, it is somebody
        else's traffic, and raising on it would make one bound gate break
        every other handler on the channel.
        """
        gate = _gate(tmp_path)
        channel = InMemoryInteractiveChannel()
        bind_gate_to_channel(gate, channel)
        action = _held(gate)

        also_ran = []
        channel.on_action(lambda o: also_ran.append(o.action_id))

        channel.inject_action("acknowledge", actor="@nick:ut")

        assert also_ran == ["acknowledge"], "the other handler still ran"
        assert gate.get(action.action_id).status == ActionStatus.PENDING

    def test_parse_rejects_what_is_not_ours(self):
        assert parse_option_id("approve:ab12") == ("approve", "ab12")
        assert parse_option_id("reject:ab12") == ("reject", "ab12")
        assert parse_option_id("acknowledge") is None
        assert parse_option_id("approve:") is None, "a prefix with no id is not an id"


class TestTheGateStaysTheSingleRecord:
    def test_a_second_click_does_not_re_decide_or_re_fire(self, tmp_path):
        """Two people click Approve. The first answer stands, once."""
        gate = _gate(tmp_path)
        channel = InMemoryInteractiveChannel()
        resumed = []
        bind_gate_to_channel(gate, channel, on_decided=resumed.append)
        action = _held(gate)

        channel.inject_action(f"approve:{action.action_id}", actor="@ben:ut")
        channel.inject_action(f"approve:{action.action_id}", actor="@nick:ut")

        assert gate.get(action.action_id).decided_by == "@ben:ut", "first answer stands"
        assert len(resumed) == 1, "a resume hook must not run twice"

    def test_a_click_for_an_action_the_gate_never_had_is_survivable(self, tmp_path):
        gate = _gate(tmp_path)
        channel = InMemoryInteractiveChannel()
        bind_gate_to_channel(gate, channel)

        channel.inject_action("approve:nosuchaction", actor="@ben:ut")

        assert gate.pending() == []


class TestCrossProcess:
    def test_the_answer_reaches_a_gate_that_did_not_ask(self, tmp_path):
        """The proposer's process is gone. The click still lands.

        This is the whole reason the store had to become durable: the notified
        human answers minutes later, into a different process.
        """
        path = tmp_path / "approvals.json"
        action_id = _held(ApprovalGate(FileActionStore(path))).action_id

        answering = ApprovalGate(FileActionStore(path))
        channel = InMemoryInteractiveChannel()
        bind_gate_to_channel(answering, channel)
        channel.inject_action(f"approve:{action_id}", actor="@ben:ut")

        assert ApprovalGate(FileActionStore(path)).get(action_id).decided_by == "@ben:ut"

    def test_pending_summary_is_digest_shaped(self, tmp_path):
        gate = _gate(tmp_path)
        _held(gate)
        rows = pending_summary(gate)
        assert len(rows) == 1
        assert set(rows[0]) == {"action_id", "summary", "created_at"}


class TestTheParamsDoNotCrossACeiling:
    """The defect this closes: a controlled payload rendered into any channel.

    The first version took a channel and posted to it, with a docstring saying
    the caller owned classification routing. That is a punt, and it bypassed
    the ceiling comparison ``admitted_for`` exists to make.
    """

    def test_params_are_withheld_when_the_channel_cannot_carry_them(self, tmp_path):
        gate = _gate(tmp_path)
        channel = InMemoryInteractiveChannel()
        action = _held(gate)

        notify_pending(
            action, channel, classification=CONTROLLED, channel_ceiling=INTERNAL
        )

        posted = channel.posts[-1].text
        assert "prd_foo.md" not in posted, "the payload must not cross the ceiling"
        assert WITHHELD in posted

    def test_the_human_is_still_told_they_are_needed(self, tmp_path):
        """Silence would be worse for both sides: nobody answers, nobody knows why."""
        gate = _gate(tmp_path)
        channel = InMemoryInteractiveChannel()
        action = _held(gate)

        notify_pending(
            action, channel, classification=CONTROLLED, channel_ceiling=INTERNAL
        )

        posted = channel.posts[-1].text
        assert "Approval needed" in posted
        assert action.action_id in posted, "and where to answer it"
        assert "axi approve ok" in posted

    def test_a_channel_that_can_carry_them_still_gets_them(self, tmp_path):
        """Withholding always would make every prompt undecidable."""
        gate = _gate(tmp_path)
        channel = InMemoryInteractiveChannel()

        notify_pending(
            _held(gate), channel, classification=CONTROLLED, channel_ceiling=CONTROLLED
        )

        assert "prd_foo.md" in channel.posts[-1].text

    def test_the_buttons_still_work_when_params_are_withheld(self, tmp_path):
        """Withholding the payload must not withhold the ability to answer."""
        gate = _gate(tmp_path)
        channel = InMemoryInteractiveChannel()
        bind_gate_to_channel(gate, channel)
        action = _held(gate)
        notify_pending(
            action, channel, classification=CONTROLLED, channel_ceiling=INTERNAL
        )

        channel.inject_action(f"approve:{action.action_id}", actor="@ben:ut")

        assert gate.get(action.action_id).status == ActionStatus.APPROVED

    def test_both_classifications_are_required(self, tmp_path):
        """Neither can be skipped by forgetting it."""
        gate = _gate(tmp_path)
        channel = InMemoryInteractiveChannel()

        with pytest.raises(TypeError):
            notify_pending(_held(gate), channel)

    @pytest.mark.parametrize(
        ("action_cls", "ceiling", "expect_withheld"),
        [
            (Classification.PUBLIC, Classification.PUBLIC, False),
            (Classification.INTERNAL, Classification.PUBLIC, True),
            (Classification.REGULATED, Classification.INTERNAL, True),
            (Classification.CONTROLLED, Classification.REGULATED, True),
            (Classification.PUBLIC, Classification.CONTROLLED, False),
        ],
    )
    def test_the_ceiling_comparison_across_every_tier(
        self, tmp_path, action_cls, ceiling, expect_withheld
    ):
        _, withheld = summarize_for(
            _held(_gate(tmp_path)), classification=action_cls, channel_ceiling=ceiling
        )
        assert withheld is expect_withheld
