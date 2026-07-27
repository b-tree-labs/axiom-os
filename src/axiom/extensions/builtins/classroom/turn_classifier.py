# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom turn classifier — core intent + learning-objective matching.

The generic intent classifier lives in `axiom.agents.turn_classifier`
(q_and_a / generative / exploratory / debugging / metacognitive / fun).
This module wraps it with classroom-specific behavior — learning-
objective keyword matching — and re-exports the generic pieces for
existing callers.

Spec: spec-classroom.md §2.8 + §5.4.
"""

from __future__ import annotations

from axiom.agents.turn_classifier import (
    LLMClassifier,
    SessionClassification,
    annotate_traces,
    deterministic_labels,
    user_text,
)
from axiom.agents.turn_classifier import (
    classify_session as _classify_session_core,
)

# Re-exports so existing classroom imports keep working
__all__ = [
    "LLMClassifier",
    "SessionClassification",
    "annotate_traces",
    "classify_session",
    "classify_batch",
    "deterministic_labels",
    "user_text",
]


# ---------------------------------------------------------------------------
# Learning-objective matching (classroom-specific)
# ---------------------------------------------------------------------------


def match_topics(text: str, learning_objectives: list[dict]) -> list[str]:
    """Match learning objectives whose keywords appear in user text."""
    topics = []
    for lo in learning_objectives:
        lo_id = lo.get("id")
        if not lo_id:
            continue
        keywords = [k.lower() for k in lo.get("keywords", [])]
        if any(k in text for k in keywords):
            topics.append(lo_id)
    return topics


# ---------------------------------------------------------------------------
# Classroom API — adds LO topics to core intent classification
# ---------------------------------------------------------------------------


def classify_session(
    turns: list[dict],
    session_id: str,
    student_id: str,
    learning_objectives: list[dict],
    classifier: LLMClassifier | None = None,
) -> SessionClassification:
    """Classify a session into intent labels + LO topics.

    LLM-backed classifier (if provided) is expected to handle LO
    matching itself and returns topics in its result. When using
    deterministic heuristics, this wrapper runs core intent labels
    and then overlays LO keyword matching.
    """
    if classifier is not None:
        # LLM path — delegate fully to core, which passes LOs through
        return _classify_session_core(
            turns=turns,
            session_id=session_id,
            principal_id=student_id,
            classifier=classifier,
            learning_objectives=learning_objectives,
        )

    # Deterministic path — core intent + classroom LO overlay
    core_result = _classify_session_core(
        turns=turns,
        session_id=session_id,
        principal_id=student_id,
        classifier=None,
    )
    text = user_text(turns)
    core_result.topics = match_topics(text, learning_objectives)
    return core_result


def classify_batch(
    sessions: dict[str, list[dict]],
    learning_objectives: list[dict],
    classifier: LLMClassifier | None = None,
) -> list[SessionClassification]:
    """Classify many classroom sessions. Pulls student_id from turns."""
    results = []
    for session_id, turns in sessions.items():
        student_id = ""
        for t in turns:
            if t.get("student_id"):
                student_id = t["student_id"]
                break
        results.append(classify_session(
            turns=turns,
            session_id=session_id,
            student_id=student_id,
            learning_objectives=learning_objectives,
            classifier=classifier,
        ))
    return results
