# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Concrete LLM-backed quiz grader (T0-4 migration).

Returns a callable matching the ``LLMGrader`` protocol in
:mod:`axiom.extensions.builtins.classroom.quiz_scoring`, built on
:func:`axiom.infra.structured_output.structured_output` so the
returned grade adheres to a fixed schema — no regex over model output.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from axiom.infra.structured_output import structured_output


class _GradeSchema(TypedDict):
    score: float
    rationale: str


_SYSTEM = (
    "You are a strict-but-fair grader for free-text quiz responses. Given "
    "a question, the student's answer, and a rubric, assign a numeric score "
    "in [0.0, 1.0] and write a one- or two-sentence rationale. Be concrete — "
    "quote the rubric dimension(s) the answer met or missed.\n\n"
    "Return the grade by calling the emit_grade tool. Do not emit plain text."
)


def build_llm_grader(
    gateway,
    tool_name: str = "emit_grade",
    max_tokens: int = 512,
) -> Callable[..., dict[str, Any]]:
    """Factory for an LLM-backed ``LLMGrader``.

    Returned callable accepts ``question``, ``answer``, ``rubric`` and
    returns ``{"score": float, "rationale": str}``. Validation is
    guaranteed by the underlying structured_output helper.
    """

    def grade(question: str, answer: str, rubric: Any, **_: Any) -> dict[str, Any]:
        prompt = (
            f"QUESTION:\n{question}\n\n"
            f"STUDENT ANSWER:\n{answer}\n\n"
            f"RUBRIC:\n{rubric}\n\n"
            "Call emit_grade with the score (0.0–1.0) and rationale."
        )
        result = structured_output(
            gateway=gateway,
            schema=_GradeSchema,
            messages=[{"role": "user", "content": prompt}],
            system=_SYSTEM,
            tool_name=tool_name,
            max_tokens=max_tokens,
            task="grading",
        )
        # Clamp score to [0, 1] as a defensive measure.
        value = dict(result.value)
        value["score"] = max(0.0, min(1.0, float(value["score"])))
        return value

    return grade
