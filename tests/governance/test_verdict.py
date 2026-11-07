# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axiom.governance.verdict`.

Per spec-governance-fabric §5.1 + prd-axiom-authz §5.1: the verdict is
the typed answer GUARD returns from `decide(envelope)`. Callers branch
on `next_action_for_caller`, never on the raw decision.
"""

from __future__ import annotations

import pytest

from axiom.governance.verdict import (
    Decision,
    NextAction,
    Verdict,
)


class TestDecisionEnum:
    @pytest.mark.parametrize("value", ["permit", "deny", "propose_to_human", "rate_limit", "expired_capability"])
    def test_value_round_trip(self, value):
        assert Decision(value).value == value

    def test_unknown_decision_rejected(self):
        with pytest.raises(ValueError):
            Decision("unknown")


class TestNextActionMapping:
    """Spec §5.1: callers branch on next_action_for_caller, not decision."""

    @pytest.mark.parametrize(
        "decision,expected_next_action",
        [
            (Decision.PERMIT, NextAction.PROCEED),
            (Decision.DENY, NextAction.ABORT),
            (Decision.PROPOSE_TO_HUMAN, NextAction.ENQUEUE_PROPOSAL),
            (Decision.RATE_LIMIT, NextAction.ABORT),
            (Decision.EXPIRED_CAPABILITY, NextAction.ABORT),
        ],
    )
    def test_decision_maps_to_next_action(self, decision, expected_next_action):
        v = Verdict.from_decision(decision, reason="test", receipt_fragment_id="r-1")
        assert v.next_action_for_caller is expected_next_action


class TestVerdictConstruction:
    def test_basic_verdict(self):
        v = Verdict(
            decision=Decision.PERMIT,
            reason="rule X matched",
            receipt_fragment_id="r-1",
            next_action_for_caller=NextAction.PROCEED,
        )
        assert v.decision is Decision.PERMIT
        assert v.is_permitted

    def test_denied_is_not_permitted(self):
        v = Verdict(
            decision=Decision.DENY,
            reason="rule X matched",
            receipt_fragment_id="r-1",
            next_action_for_caller=NextAction.ABORT,
        )
        assert not v.is_permitted

    def test_propose_is_not_permitted(self):
        v = Verdict(
            decision=Decision.PROPOSE_TO_HUMAN,
            reason="novel intent class",
            receipt_fragment_id="r-1",
            next_action_for_caller=NextAction.ENQUEUE_PROPOSAL,
        )
        assert not v.is_permitted

    def test_from_decision_helper(self):
        v = Verdict.from_decision(Decision.PERMIT, "rule X", "r-1")
        assert v.decision is Decision.PERMIT
        assert v.next_action_for_caller is NextAction.PROCEED


class TestVerdictReason:
    """The reason is the audit-trail explanation."""

    def test_reason_required(self):
        with pytest.raises(ValueError):
            Verdict(
                decision=Decision.PERMIT,
                reason="",
                receipt_fragment_id="r-1",
                next_action_for_caller=NextAction.PROCEED,
            )

    def test_receipt_required(self):
        with pytest.raises(ValueError):
            Verdict(
                decision=Decision.PERMIT,
                reason="ok",
                receipt_fragment_id="",
                next_action_for_caller=NextAction.PROCEED,
            )
