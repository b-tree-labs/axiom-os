# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-232 E1: Layer 1 capability evals in recorded-model replay mode.

The capability below is a stand-in agent loop: it consumes the recorded model
turns exactly as a live loop would consume live ones. The eval asserts rung,
result schema, verify outcome, cost and tool trace, with no tokens spent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.evals.capability_replay import (
    CapabilityCase,
    ReplayExhausted,
    ReplayModel,
    current_model,
    load_transcript,
    run_capability_case,
)
from axiom.infra.agent_declaration import AgentContract, Budget
from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec

FIXTURES = Path(__file__).parent / "fixtures" / "capability"

SCHEMA = {
    "type": "object",
    "required": ["summary", "rows"],
    "properties": {"summary": {"type": "string"}, "rows": {"type": "integer"}},
}


def _summarize(params, ctx):
    """Reference loop: ask the model, run any tool call it makes, stop on a turn with none."""
    model = current_model()
    while True:
        turn = model.next_turn()
        calls = turn.get("tool_calls") or ()
        if not calls:
            return SkillResult(value=json.loads(turn["content"]))
        for call in calls:
            model.tool_result(call["name"])


def _verify_rows(params, ctx):
    value = params["result"]
    return SkillResult(ok=value["rows"] > 0, value=value)


def _registry(verify="eval.verify_rows", budget=None) -> SkillRegistry:
    reg = SkillRegistry()
    reg.register_skill(
        SkillSpec(
            name="eval.verify_rows", fn=_verify_rows, description="Deterministic check", risk="read",
            side_effects=False, execution="deterministic",
        ),
        mutating=False,
    )
    reg.register_skill(
        SkillSpec(
            name="eval.summarize", fn=_summarize, description="Summarize a series", risk="read",
            side_effects=False, execution="agentic", model_class="simple", latency_class="bounded",
            verify=verify,
            agent=AgentContract(
                persona="analyst", allowed_tools=("data.describe",), model_class="simple",
                budget=budget or Budget(turns=4, tokens=1000, dollars=0.01, wall_clock_s=30),
                output_schema=SCHEMA, verify=verify,
            ),
        ),
        mutating=False,
    )
    return reg


def test_transcript_loads_and_rejects_a_bad_line(tmp_path):
    events = load_transcript(FIXTURES / "summarize_ok.jsonl")
    assert [e["type"] for e in events] == ["model", "tool_result", "model"]
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"type": "model"}\nnot json\n')
    with pytest.raises(ValueError, match="bad.jsonl:2"):
        load_transcript(bad)


def test_golden_case_replays_through_the_dispatcher():
    """The example eval that runs in CI: rung, schema, verify, cost and tool trace."""
    case = CapabilityCase(
        name="summarize-happy-path",
        capability="eval.summarize",
        transcript=load_transcript(FIXTURES / "summarize_ok.jsonl"),
        expect={
            "ok": True, "rung": "agentic", "schema_valid": True, "verify": "passed",
            "within_budget": True, "tools_within_allow_list": True,
            "max_turns": 2, "max_tokens": 300, "max_dollars": 0.001,
        },
    )
    outcome = run_capability_case(_registry(), case)
    assert outcome.mismatches == []
    assert (outcome.turns, outcome.tokens) == (2, 270)
    assert outcome.tool_trace == ["data.describe"]


def test_overspend_and_off_list_tool_are_reported():
    case = CapabilityCase(
        name="summarize-overspend",
        capability="eval.summarize",
        transcript=load_transcript(FIXTURES / "summarize_overspend.jsonl"),
        expect={"within_budget": True, "tools_within_allow_list": True},
    )
    outcome = run_capability_case(_registry(), case)
    assert outcome.within_budget is False
    assert outcome.tools_within_allow_list is False
    assert len(outcome.mismatches) == 2 and not outcome.passed


def test_a_false_verify_is_caught_not_trusted():
    events = [{"type": "model", "content": json.dumps({"summary": "none", "rows": 0}), "usage": {"tokens": 5}}]
    outcome = run_capability_case(_registry(), CapabilityCase("zero-rows", "eval.summarize", transcript=events))
    assert outcome.ok and outcome.schema_valid and outcome.verify == "failed"


def test_schema_violation_is_a_failed_result_not_a_repair():
    events = [{"type": "model", "content": json.dumps({"summary": 3}), "usage": {"tokens": 5}}]
    outcome = run_capability_case(_registry(), CapabilityCase("bad-shape", "eval.summarize", transcript=events))
    assert outcome.schema_valid is False


def test_replay_past_the_recording_fails_the_case_loudly():
    outcome = run_capability_case(_registry(), CapabilityCase("empty", "eval.summarize", transcript=[],
                                                              expect={"ok": True}))
    assert outcome.ok is False and any("ReplayExhausted" in e for e in outcome.errors)
    assert outcome.mismatches == ["ok: expected True, observed False"]


def test_model_raises_when_exhausted():
    with pytest.raises(ReplayExhausted):
        ReplayModel([]).next_turn()


def test_deterministic_capability_never_consults_the_model():
    reg = _registry()
    outcome = run_capability_case(
        reg, CapabilityCase("det", "eval.verify_rows", params={"result": {"rows": 3}},
                            expect={"rung": "deterministic", "max_turns": 0}),
    )
    assert outcome.rung == "deterministic" and outcome.turns == 0 and outcome.passed
