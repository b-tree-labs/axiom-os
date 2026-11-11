# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Routing-decision observer seam (shadow observation, zero behavior change).

The router exposes a module-level observer registry so extensions (e.g. the
``graduation`` builtin) can observe every RoutingDecision — the decision
*shape* only, never the classified content. Contract under test:

- Observers fire once per ``classify()`` with ``(decision, features)``.
- ``features`` carries decision-shape context only (no message text).
- A raising observer never affects the routing result (best-effort zone,
  same posture as the audit write).
- Unregistration works, and the registry is idempotent per observer.
"""

from __future__ import annotations

import pytest

from axiom.llm.router import (
    QueryRouter,
    RoutingDecision,
    RoutingTier,
    register_decision_observer,
    unregister_decision_observer,
)


@pytest.fixture
def captured():
    """Register a capturing observer for the test's duration."""
    calls: list[tuple[RoutingDecision, dict]] = []

    def observer(decision, features):
        calls.append((decision, features))

    register_decision_observer(observer)
    try:
        yield calls
    finally:
        unregister_decision_observer(observer)


SECRET_TEXT = "zx-quorbline-flux manifold telemetry"  # sentinel, never a keyword


class TestObserverNotification:
    def test_observer_fires_once_per_classify_session_path(self, captured):
        router = QueryRouter()
        decision = router.classify(SECRET_TEXT, session_mode="public")
        assert len(captured) == 1
        observed_decision, features = captured[0]
        assert observed_decision is decision
        assert observed_decision.tier == RoutingTier.PUBLIC
        assert features["session_mode"] == "public"

    def test_observer_fires_on_fallback_path(self, captured):
        # permissive skips the SLM entirely → deterministic fallback path.
        router = QueryRouter()
        decision = router.classify(
            SECRET_TEXT, session_mode="auto", sensitivity="permissive"
        )
        assert decision.classifier == "fallback"
        assert len(captured) == 1
        _, features = captured[0]
        assert features["sensitivity"] == "permissive"

    def test_features_carry_shape_not_content(self, captured):
        router = QueryRouter()
        router.classify(
            SECRET_TEXT,
            session_mode="public",
            context=[{"role": "user", "content": "another secret utterance"}],
        )
        _, features = captured[0]
        # Decision shape only: no message text may leak into the payload.
        blob = repr(features)
        assert "quorbline" not in blob
        assert "secret utterance" not in blob
        assert features["context_turns"] == 1

    def test_raising_observer_never_breaks_routing(self):
        def bad_observer(decision, features):
            raise RuntimeError("observer bug")

        register_decision_observer(bad_observer)
        try:
            decision = QueryRouter().classify(SECRET_TEXT, session_mode="public")
        finally:
            unregister_decision_observer(bad_observer)
        assert decision.tier == RoutingTier.PUBLIC

    def test_register_is_idempotent_and_unregister_is_safe(self, captured):
        def noop(decision, features):
            pass

        register_decision_observer(noop)
        register_decision_observer(noop)  # no double-registration
        unregister_decision_observer(noop)
        unregister_decision_observer(noop)  # unregistering twice is a no-op

        QueryRouter().classify(SECRET_TEXT, session_mode="public")
        # Only the fixture's observer saw the decision.
        assert len(captured) == 1
