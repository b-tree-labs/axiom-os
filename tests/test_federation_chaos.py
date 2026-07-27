# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for chaos test framework — federation resilience testing."""


from axiom.vega.federation.chaos import (
    ChaosResult,
    ChaosRunner,
    ChaosScenario,
    ScenarioType,
)


class TestChaosScenario:
    """ChaosScenario data model tests."""

    def test_to_dict(self):
        s = ChaosScenario(
            name="test",
            scenario_type=ScenarioType.NETWORK_PARTITION,
            description="A test scenario",
            steps=["step 1", "step 2"],
        )
        d = s.to_dict()
        assert d["name"] == "test"
        assert d["scenario_type"] == "network-partition"
        assert d["steps"] == ["step 1", "step 2"]


class TestChaosResult:
    """ChaosResult serialization tests."""

    def test_to_dict(self):
        r = ChaosResult(
            scenario="test",
            success=True,
            detection_time_ms=1.5,
            alerts_generated=2,
            details={"key": "value"},
            timestamp="2026-01-01T00:00:00+00:00",
        )
        d = r.to_dict()
        assert d["scenario"] == "test"
        assert d["success"] is True
        assert d["detection_time_ms"] == 1.5
        assert d["alerts_generated"] == 2
        assert d["details"] == {"key": "value"}
        assert d["data_loss"] is False

    def test_to_dict_defaults(self):
        r = ChaosResult(scenario="x", success=False)
        d = r.to_dict()
        assert d["false_positives"] == 0
        assert d["data_loss"] is False
        assert d["details"] == {}


class TestChaosRunner:
    """Integration tests for ChaosRunner."""

    def test_list_scenarios(self, tmp_path):
        runner = ChaosRunner(state_dir=tmp_path)
        scenarios = runner.list_scenarios()
        assert len(scenarios) == 6
        names = {s.name for s in scenarios}
        assert "network-partition" in names
        assert "content-injection" in names
        assert "mass-publish" in names
        assert "identity-replay" in names
        assert "ec-leak-attempt" in names
        assert "split-brain" in names

    def test_unknown_scenario(self, tmp_path):
        runner = ChaosRunner(state_dir=tmp_path)
        result = runner.run_scenario("nonexistent")
        assert not result.success
        assert "Unknown scenario" in result.details.get("error", "")

    def test_content_injection(self, tmp_path):
        runner = ChaosRunner(state_dir=tmp_path)
        result = runner.run_scenario("content-injection")
        assert result.success, f"content-injection failed: {result.details}"
        assert result.details["content_rejected"] is True
        assert result.details["alert_generated"] is True
        assert result.details["trust_degraded"] is True
        assert result.alerts_generated > 0

    def test_mass_publish(self, tmp_path):
        runner = ChaosRunner(state_dir=tmp_path)
        result = runner.run_scenario("mass-publish")
        assert result.success, f"mass-publish failed: {result.details}"
        assert result.details["mass_publish_detected"] is True
        assert result.details["trust_score"] < 1.0

    def test_ec_leak_attempt(self, tmp_path):
        runner = ChaosRunner(state_dir=tmp_path)
        result = runner.run_scenario("ec-leak-attempt")
        assert result.success, f"ec-leak-attempt failed: {result.details}"
        assert result.details["ec_blocked"] is True

    def test_identity_replay(self, tmp_path):
        runner = ChaosRunner(state_dir=tmp_path)
        result = runner.run_scenario("identity-replay")
        assert result.success, f"identity-replay failed: {result.details}"
        assert result.details["evicted_node_rejected"] is True

    def test_network_partition(self, tmp_path):
        runner = ChaosRunner(state_dir=tmp_path)
        result = runner.run_scenario("network-partition")
        assert result.success, f"network-partition failed: {result.details}"
        assert result.details["other_nodes_operational"] is True
        assert result.details["recovery_successful"] is True

    def test_split_brain(self, tmp_path):
        runner = ChaosRunner(state_dir=tmp_path)
        result = runner.run_scenario("split-brain")
        assert result.success, f"split-brain failed: {result.details}"
        assert result.data_loss is False
        assert result.details["no_data_loss"] is True

    def test_run_all(self, tmp_path):
        runner = ChaosRunner(state_dir=tmp_path)
        results = runner.run_all()
        assert len(results) == 6
        for r in results:
            assert r.success, f"{r.scenario} failed: {r.details}"
            assert r.timestamp != ""

    def test_get_results_persists(self, tmp_path):
        runner = ChaosRunner(state_dir=tmp_path)
        # Initially empty
        assert runner.get_results() == []

        # Run one scenario
        runner.run_scenario("ec-leak-attempt")

        # Results persisted
        results = runner.get_results()
        assert len(results) == 1
        assert results[0]["scenario"] == "ec-leak-attempt"
        assert results[0]["success"] is True

        # Run another — accumulates
        runner.run_scenario("identity-replay")
        results = runner.get_results()
        assert len(results) == 2
