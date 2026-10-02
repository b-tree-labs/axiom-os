# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Procedural memory effectiveness (#44 — MIRIX).

Procedural fragments track success_count and failure_count in their
content. The derived `effectiveness_score = success / (success +
failure)` is a cheap, deterministic demotion signal — procedures
that consistently fail get flagged for peer review (federated-
learning-harvest, task #20) or replaced.

Per MIRIX / Substrate-App.
"""

from __future__ import annotations

import dataclasses

from .fragment import CognitiveType, MemoryFragment

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _counts(fragment: MemoryFragment) -> tuple[int, int]:
    """Return (success_count, failure_count) from a procedural fragment."""
    c = fragment.content
    return (int(c.get("success_count", 0)), int(c.get("failure_count", 0)))


def _require_procedural(fragment: MemoryFragment) -> None:
    if fragment.cognitive_type is not CognitiveType.PROCEDURAL:
        raise ValueError(
            f"expected procedural fragment; got {fragment.cognitive_type.value}"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def effectiveness(fragment: MemoryFragment) -> float | None:
    """Return success / (success + failure) or None if no data yet."""
    if fragment.cognitive_type is not CognitiveType.PROCEDURAL:
        return None
    success, failure = _counts(fragment)
    total = success + failure
    if total == 0:
        return None
    return success / total


def record_outcome(
    fragment: MemoryFragment, succeeded: bool
) -> MemoryFragment:
    """Return a new fragment with the outcome recorded.

    Signature slot is cleared because the content (counts) changed.
    Caller re-signs if cryptographic provenance is required.
    """
    _require_procedural(fragment)
    success, failure = _counts(fragment)
    if succeeded:
        success += 1
    else:
        failure += 1
    new_content = dict(fragment.content)
    new_content["success_count"] = success
    new_content["failure_count"] = failure
    return dataclasses.replace(
        fragment,
        content=new_content,
        signature=None,
    )


def with_effectiveness_score(fragment: MemoryFragment) -> MemoryFragment:
    """Compute effectiveness and bake it into the reserved slot."""
    score = effectiveness(fragment)
    return dataclasses.replace(
        fragment,
        effectiveness_score=score,
        signature=None,
    )


def demotion_candidates(
    fragments: list[MemoryFragment],
    threshold: float = 0.5,
    min_runs: int = 3,
) -> list[MemoryFragment]:
    """Return procedural fragments whose effectiveness < threshold.

    `min_runs` filters out noise — a procedure with 0 successes and
    1 failure has effectiveness 0.0 but too little data to trust.
    """
    out = []
    for f in fragments:
        if f.cognitive_type is not CognitiveType.PROCEDURAL:
            continue
        success, failure = _counts(f)
        if (success + failure) < min_runs:
            continue
        score = effectiveness(f)
        if score is not None and score < threshold:
            out.append(f)
    return out
