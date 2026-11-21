# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The fifth approval outcome: hold, the one that does not decide.

Approve, reject and their persisting forms all answer now, which quietly
assumed a human at the keyboard. A surface with nobody watching had two
options and both were bad: prompt into the void, or let an ``ApprovalPolicy``
pre-decide, which is a rule written in advance rather than a human in the loop.

Holding keeps the action, durably, so a person answers from wherever they are.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat.agent import _hold_for_later
from axiom.extensions.builtins.chat.permissions import (
    APPROVAL_LEGEND,
    APPROVAL_RETRY,
    approval_legend,
    parse_approval_choice,
)
from axiom.infra.orchestrator.actions import Action, ActionCategory, ActionStatus
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import (
    FileActionStore,
    default_approval_path,
)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))


def _action():
    return Action(
        name="doc_publish",
        params={"source": "prd_foo.md"},
        category=ActionCategory.WRITE,
    )


class TestTheVocabulary:
    @pytest.mark.parametrize("typed", ["h", "hold", "later", "defer", "HOLD"])
    def test_the_spellings_a_person_would_reach_for(self, typed):
        assert parse_approval_choice(typed) == "h"

    def test_hold_is_not_one_shift_key_from_deny_always(self):
        """``D`` persists a refusal and has no undo. ``d`` must not be nearby.

        This is why the outcome is ``h``. A mistyped hold that becomes
        "never allow this tool again" is a bad trade for one saved keystroke.
        """
        assert parse_approval_choice("D") == "D"
        assert parse_approval_choice("d") != "D"

    def test_it_is_offered_in_the_legend_every_surface_shares(self):
        assert ("h", "old for a human") in APPROVAL_LEGEND
        assert "[h]old" in approval_legend()
        assert "[h]old" in APPROVAL_RETRY

    def test_the_existing_outcomes_are_unchanged(self):
        """A new choice may not reinterpret an old one."""
        assert parse_approval_choice("a") == "a"
        assert parse_approval_choice("A") == "A"
        assert parse_approval_choice("r") == "r"
        assert parse_approval_choice("s") == "r"
        assert parse_approval_choice("nonsense") is None


class TestHoldingReachesTheQueueThatIsRead:
    def test_a_held_action_lands_where_axi_approve_looks(self):
        """The one invariant that makes the feature real rather than plausible."""
        action = _action()

        held = _hold_for_later(action)

        assert held == action.action_id
        gate = ApprovalGate(FileActionStore(default_approval_path()))
        assert [a.action_id for a in gate.pending()] == [action.action_id]

    def test_it_survives_into_another_process_view(self, tmp_path):
        action = _action()
        _hold_for_later(action)

        fresh = ApprovalGate(FileActionStore(default_approval_path()))
        assert fresh.get(action.action_id).status == ActionStatus.PENDING
        assert fresh.get(action.action_id).params == {"source": "prd_foo.md"}

    def test_the_action_keeps_its_name_so_a_runner_can_execute_it(self):
        action = _action()
        _hold_for_later(action)

        gate = ApprovalGate(FileActionStore(default_approval_path()))
        assert gate.get(action.action_id).name == "doc_publish"


class TestAnUnreachableQueueIsNotSilent:
    def test_it_returns_none_rather_than_inventing_an_id(self, monkeypatch):
        """Telling somebody their action is queued when it is not is worse than
        telling them it failed: they wait instead of retrying."""
        monkeypatch.setattr(
            "axiom.infra.orchestrator.approval.ApprovalGate.record",
            lambda self, action: (_ for _ in ()).throw(OSError("read-only fs")),
        )

        assert _hold_for_later(_action()) is None
