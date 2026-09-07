# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""AXI-backed conversational responder — talk about anything, degrade to the
structured interview offline (ADR-074)."""

from __future__ import annotations

from axiom.extensions.builtins.diagnostics.incident_chat import make_axi_responder

_CTX = {
    "title": "Pod crash-looping: langfuse-clickhouse-shard0-0 (OOMKilled)",
    "pod": "langfuse-clickhouse-shard0-0",
    "restarts": 7059,
    "oom": True,
    "remediation_plan": {"reversible": True, "new_limit_bytes": 24 * 1024**3},
}


class _FakeAgent:
    def __init__(self):
        self.turns = []
        self.mode = None

    def set_interaction_mode(self, mode):
        self.mode = mode

    def turn(self, text, stream=True, raw=False):
        self.turns.append(text)
        return f"AXI: re '{text.splitlines()[-1]}'"


def test_routes_freeform_through_axi_and_seeds_context_once():
    agent = _FakeAgent()
    r = make_axi_responder(agent=agent)
    out = r("can you explain clickhouse memory tuning generally?", _CTX)
    assert out.startswith("AXI:")
    # first turn carries the incident preamble so AXI knows the situation
    assert "langfuse-clickhouse-shard0-0" in agent.turns[0]
    assert "awaiting their explicit approval" in agent.turns[0]
    # second turn is not re-seeded
    r("and what's the risk?", _CTX)
    assert "awaiting their explicit approval" not in agent.turns[1]


def test_falls_back_to_interview_on_empty_reply():
    class _Silent(_FakeAgent):
        def turn(self, *a, **k):
            return ""

    r = make_axi_responder(agent=_Silent(), fallback=lambda q, c: "INTERVIEW")
    assert r("what's the root cause?", _CTX) == "INTERVIEW"


def test_falls_back_when_turn_raises():
    class _Boom(_FakeAgent):
        def turn(self, *a, **k):
            raise RuntimeError("no api key")

    r = make_axi_responder(agent=_Boom(), fallback=lambda q, c: "INTERVIEW")
    assert r("anything", _CTX) == "INTERVIEW"


def test_ask_only_mode_is_set_on_injected_agent():
    agent = _FakeAgent()
    make_axi_responder(agent=agent)("hi", _CTX)
    assert agent.mode == "ask"
