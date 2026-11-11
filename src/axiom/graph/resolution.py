# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Stage 3: Entity resolution — merge duplicate entities across documents.

Determines if two entity references are the same real-world thing:
1. Exact name match + same type → AUTO-MERGE (confidence 1.0)
2. Fuzzy match (Levenshtein ≤ 2) + same type → CANDIDATE
3. Candidates above threshold are merged; below are flagged for review
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .schema import Entity

log = logging.getLogger(__name__)


@dataclass
class ResolutionResult:
    """Result of resolving one entity against existing graph."""

    entity: Entity
    merged_with: str | None = None  # Name of the entity it was merged with
    confidence: float = 1.0
    action: str = "new"  # "new", "merged", "flagged"


def resolve_entities(
    new_entities: list[Entity],
    existing_names: dict[str, str] | None = None,
    threshold: float = 0.85,
) -> list[ResolutionResult]:
    """Resolve new entities against existing ones.

    Args:
        new_entities: Entities to resolve
        existing_names: Dict of {name: label} from existing graph
        threshold: Merge confidence threshold (above = merge, below = flag)

    Returns:
        List of ResolutionResult with merge decisions
    """
    if existing_names is None:
        existing_names = {}

    results = []
    for entity in new_entities:
        # 1. Exact match
        if entity.name in existing_names:
            if existing_names[entity.name] == entity.label:
                results.append(
                    ResolutionResult(
                        entity=entity,
                        merged_with=entity.name,
                        confidence=1.0,
                        action="merged",
                    )
                )
                continue

        # 2. Fuzzy match
        best_match = None
        best_score = 0.0
        for existing_name, existing_label in existing_names.items():
            if existing_label != entity.label:
                continue
            score = _similarity(entity.name, existing_name)
            if score > best_score:
                best_score = score
                best_match = existing_name

        if best_match and best_score >= threshold:
            results.append(
                ResolutionResult(
                    entity=entity,
                    merged_with=best_match,
                    confidence=best_score,
                    action="merged",
                )
            )
        elif best_match and best_score >= 0.6:
            results.append(
                ResolutionResult(
                    entity=entity,
                    merged_with=best_match,
                    confidence=best_score,
                    action="flagged",
                )
            )
        else:
            results.append(
                ResolutionResult(
                    entity=entity,
                    confidence=1.0,
                    action="new",
                )
            )

    return results


def _similarity(a: str, b: str) -> float:
    """Compute normalized similarity between two strings.

    Uses 1 - (levenshtein_distance / max_length).
    """
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0

    # Normalize
    a_lower = a.lower().strip()
    b_lower = b.lower().strip()
    if a_lower == b_lower:
        return 0.99  # Case-only difference

    # Levenshtein distance
    dist = _levenshtein(a_lower, b_lower)
    max_len = max(len(a_lower), len(b_lower))
    return 1.0 - (dist / max_len)


def _levenshtein(s1: str, s2: str) -> int:
    """Compute Levenshtein edit distance."""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)

    if len(s2) == 0:
        return len(s1)

    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = prev_row[j + 1] + 1
            deletions = curr_row[j] + 1
            substitutions = prev_row[j] + (c1 != c2)
            curr_row.append(min(insertions, deletions, substitutions))
        prev_row = curr_row

    return prev_row[-1]
