# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-084 — STEP_UP_REQUIRED originates from the decision.

Today ``infra/stepup.py`` raises ``StepUpRequired`` *before* a floored operation,
disconnected from GUARD: the decision site and the elevation site disagree about
when a floor applies. Making step-up a verdict puts the requirement where the
decision is made, and makes it recoverable rather than terminal.
"""

from __future__ import annotations

import pytest

from axiom.governance.verdict import Challenge, Decision, NextAction, Verdict


class TestChallenge:
    def test_renders_rfc9470_www_authenticate(self):
        c = Challenge(acr_values=("axiom:posture:sso",), max_age=300.0)
        header = c.to_www_authenticate()
        assert header.startswith("Bearer ")
        assert 'error="insufficient_user_authentication"' in header
        assert 'acr_values="axiom:posture:sso"' in header
        assert 'max_age=300' in header

    def test_insufficient_scope_is_the_other_rfc9470_error(self):
        c = Challenge(error="insufficient_scope", scope="calendars.write")
        header = c.to_www_authenticate()
        assert 'error="insufficient_scope"' in header
        assert 'scope="calendars.write"' in header

    def test_multiple_acr_values_are_space_delimited(self):
        c = Challenge(acr_values=("a", "b"))
        assert 'acr_values="a b"' in c.to_www_authenticate()

    def test_max_age_zero_is_emitted_not_dropped(self):
        """0 means 'authenticate right now' — dropping it as falsy would turn the
        strictest possible demand into no demand at all."""
        assert "max_age=0" in Challenge(max_age=0.0).to_www_authenticate()

    def test_rejects_an_unknown_error_code(self):
        with pytest.raises(ValueError):
            Challenge(error="something_else")


class TestStepUpVerdict:
    CHALLENGE = Challenge(acr_values=("axiom:posture:sso",))

    def test_step_up_is_recoverable_not_terminal(self):
        """The point of the verdict: the caller can satisfy it and retry.
        Mapping it to ABORT would make an elevation prompt look like a denial."""
        v = Verdict.from_decision(
            Decision.STEP_UP_REQUIRED, "needs sso", "frag-1", challenge=self.CHALLENGE
        )
        assert v.next_action_for_caller is NextAction.SATISFY_CHALLENGE
        assert v.next_action_for_caller is not NextAction.ABORT
        assert v.is_permitted is False

    def test_step_up_without_a_challenge_is_refused(self):
        """A caller told to step up but not told HOW is stuck — so the invariant
        is enforced at construction rather than discovered at the call site."""
        with pytest.raises(ValueError):
            Verdict(
                decision=Decision.STEP_UP_REQUIRED,
                reason="needs sso",
                receipt_fragment_id="frag-1",
                next_action_for_caller=NextAction.SATISFY_CHALLENGE,
            )

    def test_a_challenge_on_any_other_decision_is_refused(self):
        """Both directions, or the field becomes decorative."""
        with pytest.raises(ValueError):
            Verdict(
                decision=Decision.PERMIT,
                reason="ok",
                receipt_fragment_id="frag-1",
                next_action_for_caller=NextAction.PROCEED,
                challenge=self.CHALLENGE,
            )

    def test_carries_max_age_for_the_adr103_re_resolution_case(self):
        """ADR-103 decision 6: when the actor is staler than a decision tolerates,
        decide() returns a re-resolution challenge instead of doing a lookup."""
        v = Verdict.from_decision(
            Decision.STEP_UP_REQUIRED,
            "actor older than max_actor_age",
            "frag-2",
            challenge=Challenge(max_age=30.0),
        )
        assert v.challenge.max_age == 30.0

    def test_existing_decisions_are_unchanged(self):
        for decision, expected in (
            (Decision.PERMIT, NextAction.PROCEED),
            (Decision.DENY, NextAction.ABORT),
            (Decision.PROPOSE_TO_HUMAN, NextAction.ENQUEUE_PROPOSAL),
            (Decision.RATE_LIMIT, NextAction.ABORT),
            (Decision.EXPIRED_CAPABILITY, NextAction.ABORT),
        ):
            v = Verdict.from_decision(decision, "r", "frag")
            assert v.next_action_for_caller is expected
            assert v.challenge is None

    def test_every_decision_has_a_next_action(self):
        """A new decision without a mapping would KeyError at runtime."""
        for decision in Decision:
            assert Verdict.from_decision(
                decision,
                "r",
                "frag",
                challenge=self.CHALLENGE
                if decision is Decision.STEP_UP_REQUIRED
                else None,
            )
