# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.agents.pipeline.derive — wire PlanPipeline to AskPipeline.

Per ADR-034 §D1: Plan derivation reuses AskPipeline. This module provides the
parser (LLM JSON response → PlanSteps), the AskHooks specialization that
biases the LLM toward producing structured plans, and the AskBackedPlanPipeline
that composes the two with AskPipeline.ask().
"""

from __future__ import annotations

import json

import pytest

from axiom.agents.pipeline.derive import (
    AskBackedPlanPipeline,
    PlanDerivationHooks,
    PlanParseError,
    parse_steps_from_response,
)
from axiom.agents.pipeline.plan import (
    Plan,
    PlanRequest,
    PlanStepGate,
)

# --------------------------------------------------------------------------
# Stubs (mirror the tests/memory/test_ask_pipeline.py pattern).
# --------------------------------------------------------------------------


class _StubRetriever:
    def __init__(self, citations=()):
        self._citations = list(citations)

    def retrieve(self, query, *, k):
        from axiom.memory.ask import Citation
        return [
            Citation(title=c["title"], text=c["text"], source_id=c["source_id"])
            for c in self._citations[:k]
        ]


class _StubLLM:
    def __init__(self, response):
        self.response = response
        self.invocations = []

    def __call__(self, *, system_blocks, user_message, task):
        self.invocations.append(
            {"system_blocks": system_blocks, "user_message": user_message, "task": task}
        )
        return self.response


@pytest.fixture
def stack(tmp_path):
    from axiom.memory.bootstrap import build_memory_stack
    return build_memory_stack(scope_id="test-derive-scope", data_root=tmp_path)


def _req(goal: str = "explain the workflow") -> PlanRequest:
    return PlanRequest(
        goal=goal,
        scope_id="test-derive-scope",
        principal_id="@ben:example-org",
        accountable_human_id="@ben:example-org",
    )


# --------------------------------------------------------------------------
# parse_steps_from_response
# --------------------------------------------------------------------------


class TestParseStepsFromResponse:
    def test_parses_minimal_step_list(self):
        response = json.dumps([
            {"intent": "step one"},
            {"intent": "step two"},
        ])
        steps = parse_steps_from_response(response)
        assert len(steps) == 2
        assert steps[0].intent == "step one"
        assert steps[1].intent == "step two"

    def test_parses_full_step_record(self):
        response = json.dumps([
            {
                "intent": "retrieve docs",
                "tool_id": "rag.retrieve",
                "inputs": {"query": "the workflow"},
                "expected_outputs": ["citations"],
                "gate": "auto",
                "reach": {
                    "reads": ["/cohort/textbooks/**"],
                    "writes": [],
                    "network": [],
                },
            },
        ])
        steps = parse_steps_from_response(response)
        assert len(steps) == 1
        s = steps[0]
        assert s.tool_id == "rag.retrieve"
        assert s.inputs == {"query": "the workflow"}
        assert s.expected_outputs == ("citations",)
        assert s.gate == PlanStepGate.AUTO
        assert s.reach.reads == ("/cohort/textbooks/**",)

    def test_strips_markdown_fences(self):
        response = (
            "```json\n"
            "[{\"intent\": \"x\"}]\n"
            "```"
        )
        steps = parse_steps_from_response(response)
        assert len(steps) == 1
        assert steps[0].intent == "x"

    def test_strips_plain_fences(self):
        response = "```\n[{\"intent\": \"x\"}]\n```"
        steps = parse_steps_from_response(response)
        assert len(steps) == 1

    def test_finds_json_in_prose(self):
        # LLM might prepend prose; parser should locate the JSON list.
        response = (
            "Here is the plan:\n"
            "[{\"intent\": \"do thing\"}]\n"
            "Hope this helps."
        )
        steps = parse_steps_from_response(response)
        assert len(steps) == 1
        assert steps[0].intent == "do thing"

    def test_malformed_json_raises(self):
        with pytest.raises(PlanParseError):
            parse_steps_from_response("not json at all")

    def test_empty_list_returns_empty_tuple(self):
        steps = parse_steps_from_response("[]")
        assert steps == ()

    def test_missing_intent_raises(self):
        with pytest.raises(PlanParseError):
            parse_steps_from_response(json.dumps([{"tool_id": "x"}]))

    def test_unknown_gate_falls_back_to_auto(self):
        response = json.dumps([
            {"intent": "x", "gate": "unknown-gate-value"},
        ])
        # Tolerant parsing: unknown gate value defaults to AUTO with a warning
        # (the warning is part of the parser contract; we don't check it here,
        # but parser must not crash on extension-defined gate strings.)
        steps = parse_steps_from_response(response)
        assert steps[0].gate == PlanStepGate.AUTO


# --------------------------------------------------------------------------
# PlanDerivationHooks
# --------------------------------------------------------------------------


class TestPlanDerivationHooks:
    def test_hooks_contribute_system_layer(self, stack):
        from axiom.infra.prompt_composer import PromptComposer
        from axiom.memory.ask import AskRequest

        hooks = PlanDerivationHooks()
        composer = PromptComposer()
        req = AskRequest(
            question="g",
            principal_id="@p:c",
            scope_id="test-derive-scope",
            mode="plan_derivation",
        )
        hooks.contribute_layers(req, composer)
        # Render to inspect layer text.
        rendered = composer.render_text()
        # System should mention "plan", "JSON", and "step" — the contract for the LLM.
        assert "plan" in rendered.lower()
        assert "json" in rendered.lower()


# --------------------------------------------------------------------------
# AskBackedPlanPipeline — end-to-end with stubs
# --------------------------------------------------------------------------


class TestAskBackedPlanPipeline:
    def test_derive_produces_plan_from_llm_json(self, stack):
        from axiom.memory.ask import AskPipeline

        llm_response = json.dumps([
            {"intent": "retrieve textbook chunks"},
            {"intent": "synthesize answer"},
        ])
        ask = AskPipeline(
            memory_stack=stack,
            retriever=_StubRetriever(),
            llm=_StubLLM(response=llm_response),
        )
        pipeline = AskBackedPlanPipeline(ask_pipeline=ask)
        plan = pipeline.derive(_req(goal="explain the workflow"))

        assert isinstance(plan, Plan)
        assert len(plan.steps) == 2
        assert plan.steps[0].intent == "retrieve textbook chunks"
        assert plan.steps[1].intent == "synthesize answer"
        assert plan.request.goal == "explain the workflow"

    def test_derive_passes_goal_as_question(self, stack):
        from axiom.memory.ask import AskPipeline

        llm = _StubLLM(response="[]")
        ask = AskPipeline(
            memory_stack=stack,
            retriever=_StubRetriever(),
            llm=llm,
        )
        pipeline = AskBackedPlanPipeline(ask_pipeline=ask)
        pipeline.derive(_req(goal="UNIQUE GOAL TEXT"))

        # The LLM should have received the goal text as the user_message.
        assert len(llm.invocations) == 1
        assert "UNIQUE GOAL TEXT" in llm.invocations[0]["user_message"]

    def test_derive_uses_derivation_hooks_system_prompt(self, stack):
        from axiom.memory.ask import AskPipeline

        llm = _StubLLM(response="[]")
        ask = AskPipeline(
            memory_stack=stack,
            retriever=_StubRetriever(),
            llm=llm,
        )
        pipeline = AskBackedPlanPipeline(ask_pipeline=ask)
        pipeline.derive(_req())

        # The system blocks should mention plan derivation contract.
        system_text = " ".join(
            str(b.get("text", "") if isinstance(b, dict) else b)
            for b in llm.invocations[0]["system_blocks"]
        ).lower()
        assert "plan" in system_text
        assert "json" in system_text

    def test_derive_with_malformed_json_raises_or_returns_empty(self, stack):
        from axiom.memory.ask import AskPipeline

        ask = AskPipeline(
            memory_stack=stack,
            retriever=_StubRetriever(),
            llm=_StubLLM(response="this is not valid json at all"),
        )
        pipeline = AskBackedPlanPipeline(ask_pipeline=ask)
        with pytest.raises(PlanParseError):
            pipeline.derive(_req())

    def test_derive_records_derived_from_citations(self, stack):
        """If retrieval surfaces citations, the resulting plan's
        derived_from list reflects them (audit trail)."""
        from axiom.memory.ask import AskPipeline

        retriever = _StubRetriever([
            {"title": "T1", "text": "...", "source_id": "frag:abc"},
            {"title": "T2", "text": "...", "source_id": "frag:def"},
        ])
        ask = AskPipeline(
            memory_stack=stack,
            retriever=retriever,
            llm=_StubLLM(response="[]"),
        )
        pipeline = AskBackedPlanPipeline(ask_pipeline=ask)
        plan = pipeline.derive(_req())

        assert "frag:abc" in plan.derived_from
        assert "frag:def" in plan.derived_from
