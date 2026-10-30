# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Shadow-mode outcome recording for the LLM tier classifier.

The contract under test (phase 1 — observation only):

- ``tier_policy`` is an exact mirror of the router's deterministic tier
  policy over decision-shape features.
- ``build_shape`` records decision shapes ONLY — no message text, no
  matched keyword terms, no reason strings.
- ``observe_routing_decision`` appends one outcome record per decision and
  can never raise into the caller.
- The live routing result is never altered by observation.
- Shadow recording is disabled under pytest unless explicitly forced, so
  test runs never taint the dogfood outcome log.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.graduation import shadow
from axiom.llm.router import (
    QueryRouter,
    RoutingDecision,
    RoutingTier,
    unregister_decision_observer,
)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Isolate the outcome log under tmp and force shadow ON for these tests."""
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AXIOM_GRADUATION_SHADOW", "1")
    shadow.reset_for_tests()
    yield tmp_path
    unregister_decision_observer(shadow.observe_routing_decision)
    shadow.reset_for_tests()


def _decision(**kwargs) -> RoutingDecision:
    defaults = dict(tier=RoutingTier.PUBLIC, reason="test", classifier="fallback")
    defaults.update(kwargs)
    return RoutingDecision(**defaults)


def _shape(**overrides) -> dict:
    base = {
        "phase": "RULE",
        "session_mode": "auto",
        "sensitivity": "balanced",
        "context_turns": 0,
        "classifier": "fallback",
        "basis": "examined",
        "keyword_matched": False,
        "slm_tier": None,
        "failure_reason": None,
    }
    base.update(overrides)
    return base


class TestTierPolicy:
    """The mirror rule reproduces the router's deterministic tier policy."""

    def test_session_override(self):
        assert shadow.tier_policy(
            _shape(classifier="session", session_mode="public")
        ) == "public"
        assert shadow.tier_policy(
            _shape(classifier="session", session_mode="export_controlled")
        ) == "export_controlled"

    def test_keyword_match_is_definitive(self):
        assert shadow.tier_policy(
            _shape(classifier="keyword", keyword_matched=True)
        ) == "export_controlled"

    def test_slm_verdict_passthrough(self):
        assert shadow.tier_policy(
            _shape(classifier="ollama", slm_tier="export_controlled")
        ) == "export_controlled"
        assert shadow.tier_policy(
            _shape(classifier="ollama", slm_tier="public")
        ) == "public"

    def test_fallback_by_sensitivity(self):
        assert shadow.tier_policy(_shape(sensitivity="strict")) == "export_controlled"
        assert shadow.tier_policy(_shape(sensitivity="balanced")) == "public"
        assert shadow.tier_policy(_shape(sensitivity="permissive")) == "public"


class TestBuildShape:
    """Decision shapes only — the postrule privacy posture: decisions, never data."""

    def test_shape_has_exactly_the_declared_fields(self):
        decision = _decision()
        shape = shadow.build_shape(decision, {"session_mode": "auto"})
        assert set(shape) == set(shadow.SHAPE_FIELDS)

    def test_no_content_leaks_into_the_shape(self):
        decision = _decision(
            tier=RoutingTier.EXPORT_CONTROLLED,
            classifier="keyword",
            reason="export-control keyword match",
            matched_terms=["SENTINEL-TERM-A", "SENTINEL-TERM-B"],
            keyword_term="SENTINEL-TERM-A",
        )
        shape = shadow.build_shape(
            decision, {"session_mode": "auto", "sensitivity": "balanced"}
        )
        blob = json.dumps(shape)
        assert "SENTINEL-TERM" not in blob
        assert "keyword match" not in blob  # reason strings excluded too
        assert shape["keyword_matched"] is True

    def test_slm_tier_only_populated_on_the_ollama_path(self):
        ollama = _decision(tier=RoutingTier.PUBLIC, classifier="ollama")
        assert shadow.build_shape(ollama, {})["slm_tier"] == "public"
        fallback = _decision(tier=RoutingTier.PUBLIC, classifier="fallback")
        assert shadow.build_shape(fallback, {})["slm_tier"] is None


class TestObserveRecords:
    def test_observe_appends_one_outcome_record(self, isolated_state):
        decision = _decision(classifier="session")
        shadow.observe_routing_decision(
            decision, {"session_mode": "public", "sensitivity": "balanced"}
        )
        records = shadow.load_outcome_records()
        assert len(records) == 1
        rec = records[0]
        assert rec["label"] == "public"
        assert rec["outcome"] == "correct"  # mirror agrees with the live decision
        assert rec["source"] == "rule"
        assert rec["input"]["phase"] == "RULE"
        assert rec["input"]["classifier"] == "session"

    def test_outcome_log_lives_under_the_extension_state_dir(self, isolated_state):
        shadow.observe_routing_decision(
            _decision(classifier="session"), {"session_mode": "public"}
        )
        log = shadow.outcome_log_path()
        assert log.exists()
        assert str(log).startswith(str(isolated_state))
        assert log.name == "outcomes.jsonl"
        assert log.parent.name == shadow.SWITCH_NAME

    def test_observe_never_raises_even_when_recording_breaks(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("recorder broken")

        monkeypatch.setattr(shadow, "_get_recorder", boom)
        # Must not raise — observation can never affect routing.
        shadow.observe_routing_decision(_decision(), {"session_mode": "auto"})


class TestEnablement:
    def test_disabled_under_pytest_unless_forced(self, monkeypatch):
        monkeypatch.delenv("AXIOM_GRADUATION_SHADOW", raising=False)
        # PYTEST_CURRENT_TEST is set right now — the guard must hold, so a
        # full-suite run never taints the real dogfood outcome log.
        assert shadow.shadow_enabled() is False

    def test_env_kill_switch(self, monkeypatch):
        monkeypatch.setenv("AXIOM_GRADUATION_SHADOW", "0")
        assert shadow.shadow_enabled() is False
        monkeypatch.setenv("AXIOM_GRADUATION_SHADOW", "1")
        assert shadow.shadow_enabled() is True

    def test_auto_install_respects_disablement(self, monkeypatch):
        monkeypatch.setenv("AXIOM_GRADUATION_SHADOW", "0")
        shadow.reset_for_tests()
        shadow.auto_install()
        from axiom.llm import router as router_mod

        assert shadow.observe_routing_decision not in router_mod._DECISION_OBSERVERS


class TestEndToEnd:
    def test_classify_produces_a_record_and_is_unaltered(self, isolated_state):
        shadow.auto_install()
        decision = QueryRouter().classify(
            "zx-quorbline flux telemetry", session_mode="public"
        )
        assert decision.tier == RoutingTier.PUBLIC  # live path unchanged
        records = shadow.load_outcome_records()
        assert len(records) == 1
        assert records[0]["label"] == "public"
        # And no content reached the log.
        assert "quorbline" not in json.dumps(records[0])

    def test_router_bootstrap_installs_the_observer_lazily(self, isolated_state):
        from axiom.llm import router as router_mod

        router_mod._OBSERVER_BOOTSTRAP_DONE = False
        try:
            QueryRouter().classify("anything at all", session_mode="public")
            assert shadow.observe_routing_decision in router_mod._DECISION_OBSERVERS
            assert len(shadow.load_outcome_records()) == 1
        finally:
            router_mod._OBSERVER_BOOTSTRAP_DONE = True


class TestPostruleBackend:
    def test_sdk_recorder_selected_when_postrule_installed(self, isolated_state):
        pytest.importorskip("postrule")
        recorder = shadow._get_recorder()
        assert recorder.backend == "postrule"
        shadow.observe_routing_decision(
            _decision(classifier="session"), {"session_mode": "public"}
        )
        records = shadow.load_outcome_records()
        assert len(records) == 1
        assert records[0]["rule_output"] == "public"

    def test_sdk_switch_stays_in_rule_phase_with_no_telemetry(self, isolated_state):
        pytest.importorskip("postrule")
        from postrule import Phase

        recorder = shadow._get_recorder()
        assert recorder.backend == "postrule"
        switch = recorder._switch
        assert switch.current_phase == Phase.RULE
        # NullEmitter — nothing is queued, sent, or shipped anywhere.
        assert all(v == 0 for v in switch.telemetry_stats().values())

    def test_fallback_recorder_when_sdk_missing(self, isolated_state, monkeypatch):
        shadow.reset_for_tests()
        monkeypatch.setattr(shadow, "_import_postrule", lambda: None)
        recorder = shadow._get_recorder()
        assert recorder.backend == "jsonl"
        shadow.observe_routing_decision(
            _decision(classifier="session"), {"session_mode": "public"}
        )
        records = shadow.load_outcome_records()
        assert len(records) == 1
        # Fallback rows carry the identical ClassificationRecord key set.
        assert {"timestamp", "input", "label", "outcome", "source", "confidence"} <= set(
            records[0]
        )
