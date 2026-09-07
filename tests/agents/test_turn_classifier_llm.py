# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for T0-4 migration #1: LLM turn classifier.

Builds a concrete ``LLMClassifier`` callable backed by
``gateway.structured_output`` so callers who want LLM-quality intent
classification don't have to hand-roll prompt+regex.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from axiom.agents.turn_classifier import classify_session
from axiom.agents.turn_classifier_llm import build_llm_classifier
from axiom.infra.gateway import CompletionResponse, ToolUseBlock


def _gateway_returning(tool_input: dict, tool_name: str = "emit_classification") -> MagicMock:
    gw = MagicMock()
    gw.complete_with_tools.return_value = CompletionResponse(
        tool_use=[ToolUseBlock(tool_id="t1", name=tool_name, input=tool_input)],
        success=True,
    )
    return gw


class TestLLMClassifier:
    def test_returns_callable(self):
        gw = MagicMock()
        classifier = build_llm_classifier(gw)
        assert callable(classifier)

    def test_maps_result_into_session_classification(self):
        gw = _gateway_returning({
            "labels": ["q_and_a", "exploratory"],
            "topics": ["neutron-physics"],
            "rationale": "user is asking definitional questions",
        })
        classifier = build_llm_classifier(gw)
        result = classify_session(
            turns=[{"role": "user", "content": "What is a neutron?"}],
            session_id="s1",
            principal_id="@alice:ut",
            classifier=classifier,
        )
        assert result.labels == ["q_and_a", "exploratory"]
        assert result.topics == ["neutron-physics"]
        assert result.rationale == "user is asking definitional questions"

    def test_custom_tool_name_is_used(self):
        gw = _gateway_returning(
            {"labels": ["q_and_a"], "topics": [], "rationale": "r"},
            tool_name="classify_intent",
        )
        classifier = build_llm_classifier(gw, tool_name="classify_intent")
        classifier(turns=[{"role": "user", "content": "x"}])
        kwargs = gw.complete_with_tools.call_args.kwargs
        assert kwargs["tools"][0]["name"] == "classify_intent"


class TestPromptingBehavior:
    def test_sends_user_turns_to_gateway(self):
        gw = _gateway_returning({"labels": [], "topics": [], "rationale": ""})
        classifier = build_llm_classifier(gw)
        classifier(turns=[
            {"role": "user", "content": "USER_FIRST_TOKEN"},
            {"role": "assistant", "content": "ASSISTANT_ECHO_TOKEN"},
            {"role": "user", "content": "USER_SECOND_TOKEN"},
        ])
        # Prompt should include both user turns but not the assistant turn.
        kwargs = gw.complete_with_tools.call_args.kwargs
        prompt = kwargs["messages"][0]["content"]
        assert "USER_FIRST_TOKEN" in prompt
        assert "USER_SECOND_TOKEN" in prompt
        assert "ASSISTANT_ECHO_TOKEN" not in prompt


class TestLearningObjectives:
    def test_learning_objectives_flow_into_prompt(self):
        gw = _gateway_returning({"labels": [], "topics": [], "rationale": ""})
        classifier = build_llm_classifier(gw)
        classifier(
            turns=[{"role": "user", "content": "q"}],
            learning_objectives=[
                {"id": "LO-01", "title": "Understand decay heat",
                 "keywords": ["decay", "heat"]},
            ],
        )
        kwargs = gw.complete_with_tools.call_args.kwargs
        prompt = kwargs["messages"][0]["content"]
        assert "LO-01" in prompt
        assert "decay" in prompt
