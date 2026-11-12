# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Concrete LLM-backed turn classifier (T0-4 migration).

Returns a callable matching the ``LLMClassifier`` protocol in
:mod:`axiom.agents.turn_classifier`, built on top of
:func:`axiom.infra.structured_output.structured_output` so schema
adherence is guaranteed — no regex parsing of model output.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from axiom.infra.structured_output import structured_output


class _ClassifySchema(TypedDict):
    labels: list  # e.g. ["q_and_a", "generative"]
    topics: list  # list of LO ids (optional)
    rationale: str


_SYSTEM = (
    "You are an intent-classification assistant. Given a transcript of "
    "user turns from a learning/operational session, identify multi-label "
    "intent categories from this set:\n"
    "  q_and_a, generative, exploratory, debugging, metacognitive, fun.\n\n"
    "If a list of learning objectives is provided, also return the IDs of "
    "any objectives the user's turns touch on. If none apply, return an "
    "empty topics list.\n\n"
    "Return your decision by calling the emit_classification tool."
)


def _user_text_block(turns: list[dict]) -> str:
    lines = []
    for t in turns:
        if t.get("role") == "user":
            lines.append(f"- {t.get('content', '')}")
    return "\n".join(lines) if lines else "(no user turns)"


def _format_objectives(los: list[dict] | None) -> str:
    if not los:
        return "(none)"
    out = []
    for lo in los:
        kw = ", ".join(lo.get("keywords", []))
        out.append(f"  - {lo.get('id', '?')}: {lo.get('title', '')} [keywords: {kw}]")
    return "\n".join(out)


def build_llm_classifier(
    gateway,
    tool_name: str = "emit_classification",
    max_tokens: int = 1024,
) -> Callable[..., dict[str, Any]]:
    """Factory for an LLM-backed ``LLMClassifier``.

    Returned callable accepts ``turns`` and optional
    ``learning_objectives``; returns a dict with ``labels``, ``topics``,
    ``rationale`` — matching the protocol expected by
    :func:`axiom.agents.turn_classifier.classify_session`.
    """

    def classifier(
        turns: list[dict],
        learning_objectives: list[dict] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        user_block = _user_text_block(turns)
        lo_block = _format_objectives(learning_objectives)
        prompt = (
            "Transcript (user turns only):\n"
            f"{user_block}\n\n"
            "Learning objectives (may be empty):\n"
            f"{lo_block}\n\n"
            "Call emit_classification with the labels that apply, matching "
            "topic IDs (from the objectives above), and a one-sentence "
            "rationale."
        )
        result = structured_output(
            gateway=gateway,
            schema=_ClassifySchema,
            messages=[{"role": "user", "content": prompt}],
            system=_SYSTEM,
            tool_name=tool_name,
            max_tokens=max_tokens,
            task="classification",
        )
        return dict(result.value)

    return classifier
