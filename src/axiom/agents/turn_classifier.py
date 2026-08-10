# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Turn/session classifier — generic intent classification (§5.4 / §2.8).

Classifies conversation turns into multi-label intent categories:
q_and_a, generative, exploratory, debugging, metacognitive, fun.

Domain-agnostic core. Extensions wrap this with their own domain
concerns — e.g. classroom's `turn_classifier.py` adds learning-
objective keyword matching on top.

Two backends:
1. Deterministic keyword heuristics — standalone, runs offline.
2. Optional LLM classifier callable — higher quality; takes
   precedence when provided.

Operates on user turns only (assistant replies are ignored for
intent classification).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class SessionClassification:
    """Result of classifying a single session."""

    session_id: str
    principal_id: str  # user/student/operator identifier
    labels: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    rationale: str | None = None


# ---------------------------------------------------------------------------
# Deterministic keyword heuristics
# ---------------------------------------------------------------------------


_GENERATIVE_VERBS = (
    "write", "generate", "create", "produce", "build", "code",
    "draft", "compose", "make me a", "make a",
)

_DEBUGGING_TOKENS = (
    "error", "exception", "traceback", "bug", "broken", "not working",
    "crash", "fails", "failed", "doesn't work", "stuck on",
)

_METACOGNITIVE_TOKENS = (
    "i'm struggling", "i'm confused", "reflect", "my approach",
    "how am i doing", "what am i missing", "why do i keep",
    "help me understand why", "what should i focus on",
)

_FUN_TOKENS = (
    "joke", "lol", "haha", "🙂", "weekend", "movie",
)


def user_text(turns: list[dict]) -> str:
    """Concatenate user turn contents, lowercased. Exported so extensions
    that want to run their own heuristics on the same text can reuse it."""
    parts = []
    for t in turns:
        if t.get("role") == "user":
            parts.append(str(t.get("content", "")))
    return "\n".join(parts).lower()


_INTENT_LABELS = (
    "q_and_a",
    "generative",
    "debugging",
    "metacognitive",
    "fun",
    "exploratory",
)


def _keyword_label_list(text: str) -> list[str]:
    """Raw keyword heuristic — the deterministic intent floor."""
    labels: list[str] = []

    if "?" in text:
        labels.append("q_and_a")
    if any(v in text for v in _GENERATIVE_VERBS):
        labels.append("generative")
    if any(tok in text for tok in _DEBUGGING_TOKENS):
        labels.append("debugging")
    if any(tok in text for tok in _METACOGNITIVE_TOKENS):
        labels.append("metacognitive")
    if any(tok in text for tok in _FUN_TOKENS):
        labels.append("fun")

    if not labels:
        labels.append("exploratory")

    return labels


# ---------------------------------------------------------------------------
# Public classification entry point
# ---------------------------------------------------------------------------


def deterministic_labels(text: str) -> list[str]:
    """Apply keyword heuristics to extract multi-label intent categories."""
    return _keyword_label_list(text)


# ---------------------------------------------------------------------------
# Classifier protocol
# ---------------------------------------------------------------------------


LLMClassifier = Callable[..., dict]
"""Signature: (turns, **kw) → {labels, topics, rationale}"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_session(
    turns: list[dict],
    session_id: str,
    principal_id: str,
    classifier: LLMClassifier | None = None,
    **classifier_kwargs,
) -> SessionClassification:
    """Classify a session into intent labels.

    If `classifier` is supplied, it takes precedence — called with
    (turns=..., **classifier_kwargs) and expected to return
    {labels, topics, rationale}.
    """
    if classifier is not None:
        result = classifier(turns=turns, **classifier_kwargs)
        return SessionClassification(
            session_id=session_id,
            principal_id=principal_id,
            labels=list(result.get("labels", [])),
            topics=list(result.get("topics", [])),
            rationale=result.get("rationale"),
        )

    text = user_text(turns)
    return SessionClassification(
        session_id=session_id,
        principal_id=principal_id,
        labels=deterministic_labels(text),
        topics=[],  # core classifier doesn't know about domain topics
        rationale="deterministic keyword heuristics",
    )


def classify_batch(
    sessions: dict[str, list[dict]],
    classifier: LLMClassifier | None = None,
    principal_id_key: str = "principal_id",
    **classifier_kwargs,
) -> list[SessionClassification]:
    """Classify many sessions in one pass.

    `sessions` is {session_id: [turns]}. Principal id is pulled from
    the first turn that has `principal_id_key` (defaults to
    "principal_id"; classroom overrides with "student_id").
    """
    results = []
    for session_id, turns in sessions.items():
        pid = ""
        for t in turns:
            if t.get(principal_id_key):
                pid = t[principal_id_key]
                break
        results.append(
            classify_session(
                turns=turns,
                session_id=session_id,
                principal_id=pid,
                classifier=classifier,
                **classifier_kwargs,
            )
        )
    return results


def annotate_traces(
    traces: list[dict],
    classifications: list[SessionClassification],
) -> list[dict]:
    """Propagate labels + topics from classifications to each trace in
    the session. Returns a new list (does not mutate input)."""
    by_session = {c.session_id: c for c in classifications}
    out = []
    for t in traces:
        sid = t.get("session_id")
        new_t = dict(t)
        c = by_session.get(sid) if sid else None
        if c:
            new_t["labels"] = list(c.labels)
            new_t["topics"] = list(c.topics)
        out.append(new_t)
    return out
