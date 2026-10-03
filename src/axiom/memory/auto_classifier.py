# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shape-based auto-classifier — cheap pre-routing for MemoryFragment.

Per MIRIX / Substrate-App (classifier.go): inspect content keys and
shape to guess which CognitiveType the fragment should belong to.
Runs before the expensive LLM-validated classifier — covers the
obvious cases deterministically (no LLM call needed).

Precedence (higher = stronger signal, checked first):

    VAULT       — archive flag + retention_period (long-term storage)
    CORE        — essential=True (agent identity / system invariants)
    PROCEDURAL  — has 'steps' or 'workflow_name'
    RESOURCE    — has 'ref' / 'url' / 'file_path'
    EPISODIC    — has 'event_time' or 'timestamp'
    SEMANTIC    — has 'fact' / 'concept' / 'definition' / fallback

Returns None for empty content; the caller may run the LLM
classifier or route to review.

Confidence is a coarse 0.0–1.0 heuristic reflecting how distinctive
the signal is. Callers use it to decide whether to skip the
LLM-validated second pass (>= 0.9 ≈ skip).
"""

from __future__ import annotations

from .fragment import CognitiveType

# ---------------------------------------------------------------------------
# Signal detectors (ordered by precedence)
# ---------------------------------------------------------------------------


def _is_vault(content: dict) -> float:
    if content.get("archived") is True and "retention_period" in content:
        return 0.95
    if content.get("archived") is True:
        return 0.7
    return 0.0


def _is_core(content: dict) -> float:
    if content.get("essential") is True:
        return 0.9
    return 0.0


def _is_procedural(content: dict) -> float:
    if "steps" in content:
        return 0.95
    if "workflow_name" in content or "workflow" in content:
        return 0.8
    return 0.0


def _is_resource(content: dict) -> float:
    if "ref" in content:
        return 0.95
    if "url" in content or "file_path" in content:
        return 0.9
    return 0.0


def _is_episodic(content: dict) -> float:
    if "event_time" in content:
        return 0.9
    if "timestamp" in content:
        return 0.85
    return 0.0


def _is_semantic(content: dict) -> float:
    if "fact" in content or "concept" in content:
        return 0.9
    if "definition" in content:
        return 0.85
    if "text" in content or "content" in content:
        return 0.4  # weak fallback — any text defaults here
    return 0.0


# Ordered list of (type, detector) in precedence order
_DETECTORS = [
    (CognitiveType.VAULT, _is_vault),
    (CognitiveType.CORE, _is_core),
    (CognitiveType.PROCEDURAL, _is_procedural),
    (CognitiveType.RESOURCE, _is_resource),
    (CognitiveType.EPISODIC, _is_episodic),
    (CognitiveType.SEMANTIC, _is_semantic),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_shape_with_confidence(
    content: dict,
) -> tuple[CognitiveType | None, float]:
    """Return (type, confidence) for shape-based classification.

    (None, 0.0) if no signal at all.
    """
    if not content:
        return (None, 0.0)

    # Walk detectors in precedence order; the first non-zero wins.
    # This preserves semantics like "procedural with event_time stays
    # procedural" — the stronger categorical signal takes precedence
    # over the weaker temporal signal.
    result: tuple[CognitiveType | None, float] = (None, 0.0)
    for ct, detector in _DETECTORS:
        score = detector(content)
        if score > 0:
            result = (ct, score)
            break

    return result


def classify_shape(content: dict) -> CognitiveType | None:
    """Return best-guess CognitiveType based on content shape."""
    ct, _ = classify_shape_with_confidence(content)
    return ct
