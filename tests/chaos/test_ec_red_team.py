# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Red team: does export-control routing hold up when attacked?

Not "does it work on the happy path" — it does. These probe the paths an
auditor would ask about: what happens when the thing that decides is broken,
absent, unsure, or bypassed.

WHAT HOLDS
- A caller cannot choose its own tier. `routing_tier` is never read from a
  request; it is classified server-side.
- Provider selection is fail-closed: an export-controlled request is never
  downgraded to a non-EC provider, not by `prefer`, not by the prefer chain.
- `raw` does not bypass it. The classifier runs before the raw branch and the
  tier is forwarded to the gateway (test_raw_does_not_bypass_ec.py).

WHAT DOES NOT
- Classification fails OPEN below `strict`. The default is `balanced`, and on
  the node `routing.sensitivity` is unset — so the deployed posture is
  fail-open.
- A classifier that COULD NOT BE REACHED produces the same decision, and the
  same human-readable reason, as one that confidently cleared the content.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from axiom.llm.router import (
    SENSITIVITY_BALANCED,
    SENSITIVITY_PERMISSIVE,
    SENSITIVITY_STRICT,
    ClassifierFailure,
    QueryRouter,
)

#: Export-control relevant, deliberately using no obvious keyword — the case
#: where the SLM is the only thing standing between the content and a public
#: provider.
UNKEYWORDED = (
    "Summarise the cascade staging arrangement and the separative work "
    "figures for the configuration we discussed, with rotor tolerances."
)

CLASSIFIER_DOWN = ClassifierFailure(
    reason="ollama_unreachable",
    detail="connection refused",
    endpoint="http://127.0.0.1:11434",
    model="qwen",
)


def _decide(sensitivity, classifier_result):
    router = QueryRouter()
    with patch.object(router, "_resolve_sensitivity", return_value=sensitivity), \
         patch.object(router._ollama, "classify", return_value=classifier_result):
        return router.classify(UNKEYWORDED, session_mode="", context=[])


class TestTheDefaultPostureFailsOpen:
    """The finding. Recorded as the CURRENT behaviour so that changing it is a
    deliberate, visible act rather than a silent one."""

    @pytest.mark.parametrize(
        "sensitivity", [SENSITIVITY_PERMISSIVE, SENSITIVITY_BALANCED]
    )
    def test_a_dead_classifier_routes_to_public(self, sensitivity):
        assert _decide(sensitivity, CLASSIFIER_DOWN).tier.value == "public"

    @pytest.mark.parametrize(
        "sensitivity", [SENSITIVITY_PERMISSIVE, SENSITIVITY_BALANCED]
    )
    def test_an_uncertain_classifier_routes_to_public(self, sensitivity):
        assert _decide(sensitivity, "uncertain").tier.value == "public"

    def test_strict_is_the_only_fail_closed_posture(self):
        assert _decide(SENSITIVITY_STRICT, CLASSIFIER_DOWN).tier.value == (
            "export_controlled"
        )
        assert _decide(SENSITIVITY_STRICT, "uncertain").tier.value == (
            "export_controlled"
        )

    def test_the_default_when_nothing_is_configured_is_balanced(self):
        """Which is to say: fail-open unless someone chose otherwise. The node
        has `routing.sensitivity` unset."""
        router = QueryRouter()
        with patch(
            "axiom.extensions.builtins.settings.store.SettingsStore.get",
            side_effect=lambda key, default=None: default,
        ):
            assert router._resolve_sensitivity(None) == SENSITIVITY_BALANCED


class TestAFailureIsNotDistinguishableFromAClearance:
    """The sharpest finding, because it is what an auditor reads.

    "We checked and it was public" and "we could not check" must not produce
    the same sentence. The structured `classifier_failure` field does carry
    the difference — but the reason a human reads does not.
    """

    def test_a_failure_no_longer_reads_as_a_clearance(self):
        """FIXED by P1 (2026-09-08). This test previously asserted the two
        reasons were IDENTICAL, and said in its own message that changing that
        meant the finding was fixed. It is, so this now asserts the fix.

        The TIER is deliberately still the same — P1 is reporting only, and
        conservative selection (P3) must not land before local-only providers
        exist, or a classifier outage becomes a total outage.
        """
        cleared = _decide(SENSITIVITY_BALANCED, "public-ish")
        unchecked = _decide(SENSITIVITY_BALANCED, CLASSIFIER_DOWN)

        assert cleared.reason != unchecked.reason
        assert cleared.reason == "no export-control terms detected"
        assert "NOT examined" in unchecked.reason, unchecked.reason
        assert "ollama_unreachable" in unchecked.reason

        # Still the same tier: the behaviour change is P3, not P1.
        assert cleared.tier == unchecked.tier

    def test_the_classifier_field_keeps_its_closed_set(self):
        """`classifier` is a closed set other code switches on. The
        examined/unchecked distinction rides on `basis`, deliberately, so
        adding a fifth value here is a contract change P1 does not need."""
        assert _decide(SENSITIVITY_BALANCED, CLASSIFIER_DOWN).classifier in {
            "session", "keyword", "ollama", "fallback",
        }

    def test_the_basis_says_whether_anything_looked(self):
        assert _decide(SENSITIVITY_BALANCED, "public-ish").basis == "examined"
        assert _decide(SENSITIVITY_BALANCED, CLASSIFIER_DOWN).basis == "unchecked"

    def test_a_confident_decision_is_never_marked_unchecked(self):
        """Pinned so `basis` cannot become a constant and stop meaning
        anything."""
        from axiom.llm.router import RoutingTier

        ec = _decide(SENSITIVITY_STRICT, "uncertain")
        assert ec.tier == RoutingTier.EXPORT_CONTROLLED
        assert ec.basis == "examined", "the SLM answered; it was not unchecked"

    def test_the_structured_field_does_carry_the_difference(self):
        """So the fix is reporting, not detection: the data already exists."""
        cleared = _decide(SENSITIVITY_BALANCED, "public-ish")
        unchecked = _decide(SENSITIVITY_BALANCED, CLASSIFIER_DOWN)
        assert cleared.classifier_failure is None
        assert unchecked.classifier_failure is not None


class TestWhatDoesHold:
    """Pinned so a regression in the parts that DO work is caught."""

    def test_an_ec_request_is_never_downgraded_by_provider_preference(self):
        import inspect

        from axiom.llm import gateway

        source = inspect.getsource(gateway)
        assert "EC may not be downgraded" in source
        assert source.count('routing_tier == "export_controlled"') >= 2

    def test_the_transport_cannot_choose_the_tier(self):
        """A caller-supplied tier would make every other control decorative."""
        import inspect

        from axiom.extensions.builtins.http import chat_server

        source = inspect.getsource(chat_server)
        assert "routing_tier" not in source, (
            "the HTTP surface must never read a tier from the request"
        )
