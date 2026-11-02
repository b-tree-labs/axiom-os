# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the simple-tier failure-narrative path used by RIVET.

When CI fails with a reason that doesn't match the learned-pattern DB,
RIVET sends the failure to the LLM with persona-loaded framing. These
tests pin the persona-into-system-prompt wire and the failure-degrades
behavior when the gateway is unavailable.
"""

from __future__ import annotations

from pathlib import Path


class TestNarrativeForFailure:
    def test_returns_none_when_failure_reason_is_empty(self):
        from axiom.extensions.builtins.release.failure_narrative import (
            narrative_for_failure,
        )

        class FakeGateway:
            available = True

            def complete(self, **kwargs):
                raise AssertionError("LLM should not be called for empty failure")

        result = narrative_for_failure(
            {"repo": "x", "ref": "main", "failure_reason": ""},
            gateway=FakeGateway(),
        )
        assert result is None

    def test_returns_none_when_gateway_unavailable(self):
        from axiom.extensions.builtins.release.failure_narrative import (
            narrative_for_failure,
        )

        class FakeGateway:
            available = False

        result = narrative_for_failure(
            {"repo": "x", "ref": "main", "failure_reason": "boom"},
            gateway=FakeGateway(),
        )
        assert result is None

    def test_persona_lands_in_system_prompt(self):
        from axiom.extensions.builtins.release.failure_narrative import (
            narrative_for_failure,
        )

        persona_path = (
            Path(__file__).parent.parent / "agents" / "rivet" / "persona.md"
        )
        persona_text = persona_path.read_text(encoding="utf-8").strip()
        first_heading = persona_text.split("\n", 1)[0]

        captured: dict = {}

        class FakeResponse:
            text = '{"diagnosis": "test", "fix": "test", "confidence": "low"}'
            model = "gemma2:2b"

        class FakeGateway:
            available = True

            def complete(self, **kwargs):
                captured["system"] = kwargs.get("system", "")
                captured["prompt"] = kwargs.get("prompt", "")
                captured["task"] = kwargs.get("task", "")
                return FakeResponse()

        result = narrative_for_failure(
            {
                "repo": "axiom",
                "ref": "main",
                "url": "https://example.com/run/1",
                "failure_reason": "ImportError: no module named foo",
            },
            gateway=FakeGateway(),
        )

        assert result is not None
        assert "raw" in result
        assert first_heading in captured["system"]
        assert captured["task"] == "rivet"
        assert "ImportError" in captured["prompt"]


class TestHeartbeatAttachesNarrative:
    def test_heartbeat_includes_narrative_for_unmatched_failure(
        self, tmp_path, monkeypatch
    ):
        # Redirect agent state to tmp; stub check_pipelines + Gateway.
        import json

        from axiom.extensions.builtins.release import agent_cli, ci_monitor
        from axiom.extensions.builtins.release.ci_monitor import PipelineStatus

        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))

        def fake_check_pipelines():
            return [
                PipelineStatus(
                    repo="axiom",
                    provider="github",
                    ref="main",
                    status="failed",
                    url="https://example.com/run/1",
                    failure_reason="weird new failure with no known pattern",
                )
            ]

        monkeypatch.setattr(ci_monitor, "check_pipelines", fake_check_pipelines)
        monkeypatch.setattr(agent_cli, "check_pipelines", fake_check_pipelines, raising=False)

        class FakeResponse:
            text = '{"diagnosis": "stub", "fix": "stub", "confidence": "low"}'
            model = "gemma2:2b"

        class FakeGateway:
            available = True

            def complete(self, **kwargs):
                return FakeResponse()

        # Patch the Gateway constructor used by failure_narrative.

        monkeypatch.setattr(
            "axiom.infra.gateway.Gateway",
            lambda *a, **kw: FakeGateway(),
        )

        rc = agent_cli.main(["heartbeat"])
        assert rc in (0, 2)  # 2 = pipelines red (expected here)

        log_path = tmp_path / "state" / "agents" / "rivet" / "heartbeat.jsonl"
        assert log_path.exists()
        lines = log_path.read_text().splitlines()
        last = json.loads(lines[-1])
        unmatched = last.get("unmatched_failures", [])
        assert len(unmatched) == 1
        assert "narrative" in unmatched[0]
        assert unmatched[0]["narrative"]["raw"]
