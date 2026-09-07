# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for BaseAgent self-managed runtime + health contract (#30)."""

from __future__ import annotations

import time

import pytest


class TestVitals:
    def test_initial_state(self):
        from axiom.agents.base_agent import AgentVitals

        v = AgentVitals()
        assert v.error_count == 0
        assert v.restart_count == 0
        assert v.uptime_seconds() >= 0
        assert v.mean_latency_ms() is None

    def test_record_latency_caps_samples(self):
        from axiom.agents.base_agent import AgentVitals

        v = AgentVitals(max_latency_samples=3)
        for i in range(10):
            v.record_latency(float(i))
        assert len(v.recent_latency_ms) == 3

    def test_mean_latency(self):
        from axiom.agents.base_agent import AgentVitals

        v = AgentVitals()
        for x in [10.0, 20.0, 30.0]:
            v.record_latency(x)
        assert v.mean_latency_ms() == 20.0


class TestStatus:
    def test_healthy_by_default(self):
        from axiom.agents.base_agent import AgentStatus, BaseAgent

        agent = BaseAgent(agent_id="test")
        assert agent.status == AgentStatus.HEALTHY

    def test_degraded_on_error_threshold(self):
        from axiom.agents.base_agent import AgentStatus, BaseAgent

        agent = BaseAgent(agent_id="test", error_rate_degraded=3, max_restarts=0)
        # Errors without restart
        for _ in range(3):
            agent.vitals.record_error()
        # max_restarts=0 → restart fails, but error count stays at 3,
        # status derivation sees error_count >= threshold
        assert agent.status in (AgentStatus.DEGRADED, AgentStatus.FAILED)

    def test_idle_triggers_degraded(self):
        from axiom.agents.base_agent import AgentStatus, BaseAgent

        agent = BaseAgent(agent_id="test", idle_degraded_seconds=0.0001)
        time.sleep(0.01)
        assert agent.status == AgentStatus.DEGRADED


class TestRestart:
    def test_bounded_restart(self):
        from axiom.agents.base_agent import AgentStatus, BaseAgent

        agent = BaseAgent(agent_id="test", max_restarts=2)
        assert agent.restart() is True  # first
        assert agent.vitals.restart_count == 1
        assert agent.restart() is True  # second
        assert agent.restart() is False  # beyond limit
        assert agent.status == AgentStatus.FAILED

    def test_restart_resets_error_count(self):
        from axiom.agents.base_agent import BaseAgent

        agent = BaseAgent(agent_id="test", max_restarts=3)
        agent.vitals.record_error()
        agent.vitals.record_error()
        agent.restart()
        assert agent.vitals.error_count == 0

    def test_on_restart_hook_called(self):
        from axiom.agents.base_agent import BaseAgent

        class MyAgent(BaseAgent):
            def __init__(self, **kw):
                super().__init__(**kw)
                self.restarted = 0

            def _on_restart(self):
                self.restarted += 1

        agent = MyAgent(agent_id="test")
        agent.restart()
        assert agent.restarted == 1


class TestRunStep:
    def test_successful_step_records_latency(self):
        from axiom.agents.base_agent import BaseAgent

        agent = BaseAgent(agent_id="test")

        def work():
            return 42

        assert agent.run_step(work) == 42
        assert agent.vitals.error_count == 0
        assert len(agent.vitals.recent_latency_ms) == 1

    def test_failing_step_records_error_and_reraises(self):
        from axiom.agents.base_agent import BaseAgent

        agent = BaseAgent(agent_id="test", error_rate_degraded=100)

        def work():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            agent.run_step(work)
        assert agent.vitals.error_count == 1

    def test_error_threshold_triggers_restart(self):
        from axiom.agents.base_agent import BaseAgent

        agent = BaseAgent(
            agent_id="test",
            error_rate_degraded=2,
            max_restarts=3,
        )

        def work():
            raise RuntimeError("fail")

        with pytest.raises(RuntimeError):
            agent.run_step(work)
        with pytest.raises(RuntimeError):
            agent.run_step(work)
        # After two errors, restart fires and resets error_count
        assert agent.vitals.restart_count == 1
        assert agent.vitals.error_count == 0


class TestHealthPayload:
    def test_health_is_serializable(self):
        import json

        from axiom.agents.base_agent import BaseAgent

        agent = BaseAgent(agent_id="test")
        agent.vitals.record_latency(10.0)
        agent.vitals.set_queue_depth("work_queue", 3)
        payload = agent.health()
        s = json.dumps(payload)
        assert "test" in s
        assert payload["status"] == "healthy"
        assert payload["vitals"]["queue_depths"]["work_queue"] == 3
