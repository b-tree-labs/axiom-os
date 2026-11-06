# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for T0-4 migration #2: LLM quiz grader."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.classroom.quiz_scoring import auto_score
from axiom.extensions.builtins.classroom.quiz_scoring_llm import build_llm_grader
from axiom.infra.gateway import CompletionResponse, ToolUseBlock


def _gateway_returning(tool_input: dict, tool_name: str = "emit_grade") -> MagicMock:
    gw = MagicMock()
    gw.complete_with_tools.return_value = CompletionResponse(
        tool_use=[ToolUseBlock(tool_id="t1", name=tool_name, input=tool_input)],
        success=True,
    )
    return gw


class TestLLMGrader:
    def test_returns_score_and_rationale(self):
        gw = _gateway_returning({"score": 0.85, "rationale": "clear, complete"})
        grader = build_llm_grader(gw)
        out = grader(
            question="What is critical mass?",
            answer="Minimum mass for a sustained chain reaction.",
            rubric="Must mention sustained reaction.",
        )
        assert out["score"] == pytest.approx(0.85)
        assert "clear" in out["rationale"]

    def test_clamps_score_to_unit_interval(self):
        gw = _gateway_returning({"score": 1.5, "rationale": "bug"})
        grader = build_llm_grader(gw)
        out = grader(question="q", answer="a", rubric="r")
        assert out["score"] == 1.0

    def test_custom_tool_name(self):
        gw = _gateway_returning(
            {"score": 0.5, "rationale": "r"},
            tool_name="grade_free_text",
        )
        grader = build_llm_grader(gw, tool_name="grade_free_text")
        grader(question="q", answer="a", rubric="r")
        kwargs = gw.complete_with_tools.call_args.kwargs
        assert kwargs["tools"][0]["name"] == "grade_free_text"


class TestAutoScoreIntegration:
    """Confirm auto_score wires the new LLM grader through the existing
    free-text path without changing observable behavior."""

    def test_free_text_gets_suggested_score(self):
        gw = _gateway_returning({"score": 0.7, "rationale": "mostly there"})
        grader = build_llm_grader(gw)
        response = {
            "question_type": "free_text",
            "question_id": "q1",
            "student_id": "s1",
            "assessment_id": "a1",
            "question": "Define critical mass.",
            "answer": "Minimum mass for sustained chain reaction.",
            "rubric": "Must mention sustained reaction.",
        }
        scored = auto_score(response, answer_key={}, llm_grader=grader)
        assert scored.suggested_score == pytest.approx(0.7)
        assert scored.rationale == "mostly there"
        assert scored.needs_review is True  # always queue for instructor
