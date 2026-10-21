# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Rubric for scoring CHALKE responses in the simulator (#69).

Per-turn rubric with deterministic + optional LLM-judge components.
Emits a RubricScore with sub-scores + rationale for downstream
analysis (CURIO Karpathy tuning, Prague readiness validation).

Deterministic checks (always run):
- has_citation: response mentions "[source]" / "[chunk]" / cites any pack
- addresses_query: response word-count >= threshold relative to query
- profile_aligned: tone matches student's pedagogy_preference
- no_refusal: response isn't a canned "I cannot help" refusal
- intent_aligned: strategy matches intent per RPE rules

Optional LLM-judge (when rubric.llm_judge callable supplied):
- factual_correctness: LLM checks response against ground-truth answer
- pedagogical_appropriateness: LLM rates tone fit for student

Each sub-score is 0.0–1.0; the composite is a weighted average.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

LLMJudge = Callable[..., dict]
"""Signature: (query, response, ground_truth, student_profile, **kw) → {score: float, ...}"""


@dataclass
class RubricScore:
    """Structured per-response score with sub-scores + rationale."""

    query: str
    response: str
    student_id: str

    # Deterministic sub-scores
    has_citation: float = 0.0
    addresses_query: float = 0.0
    profile_aligned: float = 0.0
    no_refusal: float = 0.0
    intent_aligned: float = 0.0

    # Optional LLM-judge sub-scores
    factual_correctness: float | None = None
    pedagogical_appropriateness: float | None = None

    # Aggregates
    composite: float = 0.0
    rationale: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Deterministic heuristics
# ---------------------------------------------------------------------------


def _has_citation(response: str) -> float:
    """True if response cites a source (numbered ref or explicit source)."""
    indicators = ["[", "(source:", "per the", "according to", "see "]
    hit = any(ind.lower() in response.lower() for ind in indicators)
    return 1.0 if hit else 0.0


def _addresses_query(query: str, response: str) -> float:
    """Coarse signal: response ≥3x query length suggests actual engagement."""
    if not query or not response:
        return 0.0
    ratio = len(response) / max(len(query), 1)
    if ratio < 1.0:
        return 0.2
    if ratio < 3.0:
        return 0.6
    return 1.0


def _profile_aligned(student_profile: dict, response: str) -> float:
    """Check tonal signals match pedagogy_preference."""
    pref = student_profile.get("pedagogy_preference", "didactic")
    lower = response.lower()
    if pref == "socratic":
        # Socratic style should pose questions back
        return 1.0 if "?" in response else 0.4
    if pref == "didactic":
        # Didactic expects structure: numbered steps, "first, second"
        structural = any(m in lower for m in ["first", "second", "step", "next,"])
        return 1.0 if structural else 0.5
    if pref == "direct":
        # Direct wants concise — penalize rambling
        return 1.0 if len(response) < 600 else 0.5
    return 0.5


def _no_refusal(response: str) -> float:
    """Flag canned-refusal responses ('I cannot...', 'As an AI...')."""
    refusal_markers = [
        "i cannot", "i can't help", "i'm unable to",
        "as an ai language model", "i apologize, but",
    ]
    if any(m in response.lower() for m in refusal_markers):
        return 0.0
    return 1.0


def _intent_aligned(intent_id: str, response: str) -> float:
    """Intent-shape heuristics — teaching should explain; lookup should be short."""
    r = response
    if intent_id == "lookup":
        return 1.0 if len(r) < 400 else 0.5
    if intent_id == "teaching":
        return 1.0 if len(r) > 200 else 0.4
    if intent_id == "metacognitive":
        # Should reference the student's past activity
        return 1.0 if any(
            w in r.lower() for w in ["your", "you've", "you have", "based on"]
        ) else 0.4
    return 0.7  # neutral for other intents


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------


def _composite_score(rs: RubricScore) -> float:
    """Weighted average of sub-scores."""
    weights = {
        "has_citation": 0.15,
        "addresses_query": 0.15,
        "profile_aligned": 0.20,
        "no_refusal": 0.10,
        "intent_aligned": 0.15,
        "factual_correctness": 0.15,
        "pedagogical_appropriateness": 0.10,
    }
    total = 0.0
    weight_sum = 0.0
    for field_name, w in weights.items():
        v = getattr(rs, field_name)
        if v is None:
            continue
        total += v * w
        weight_sum += w
    return total / weight_sum if weight_sum > 0 else 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def score_response(
    query: str,
    response: str,
    student_id: str,
    student_profile: dict,
    intent_id: str = "teaching",
    ground_truth: str | None = None,
    llm_judge: LLMJudge | None = None,
) -> RubricScore:
    """Score a CHALKE response against the rubric.

    Deterministic sub-scores always populate. LLM-judge sub-scores
    populate only when a judge callable is provided.
    """
    rs = RubricScore(
        query=query, response=response, student_id=student_id,
        has_citation=_has_citation(response),
        addresses_query=_addresses_query(query, response),
        profile_aligned=_profile_aligned(student_profile, response),
        no_refusal=_no_refusal(response),
        intent_aligned=_intent_aligned(intent_id, response),
    )

    if llm_judge is not None:
        j = llm_judge(
            query=query, response=response,
            ground_truth=ground_truth,
            student_profile=student_profile,
            intent_id=intent_id,
        )
        rs.factual_correctness = j.get("factual_correctness")
        rs.pedagogical_appropriateness = j.get("pedagogical_appropriateness")
        if "rationale" in j:
            rs.rationale.append(j["rationale"])

    rs.composite = _composite_score(rs)
    return rs
