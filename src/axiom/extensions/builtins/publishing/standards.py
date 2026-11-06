# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PRESS standards — named skill bundles per ADR-058.

A standard is a stable operator-readable name (``publish_prd``,
``publish_for_review``) mapped to an ordered sequence of PRESS skill
invocations. Operators say "PRESS, do this the standard way" and
PRESS resolves the name to the bundle, runs it, and returns a composed
result.

CLI: ``axi pub standards`` lists; ``axi pub do <name> [args]`` runs.
A2A: peer agents invoke the same registry.
MCP: ``axiom_press__standards`` + ``axiom_press__do_<name>`` (M4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PressStandard:
    """One named skill bundle.

    ``skills`` is an ordered tuple of ``(skill_name, params_overlay)``.
    The composer threads params through each step, collecting per-step
    results into a composed receipt.
    """

    name: str
    description: str
    skills: tuple[tuple[str, dict[str, Any]], ...]
    version: str = "v1"
    category: str = "publishing"
    tags: tuple[str, ...] = field(default_factory=tuple)


STANDARDS: dict[str, PressStandard] = {
    "publish_prd": PressStandard(
        name="publish_prd",
        description=(
            "Detect the source's version metadata, then render a draft "
            "to the source's filesystem scope (non-clobbering, mirror "
            "structure, Mermaid pre-rendered)."
        ),
        skills=(
            ("press.detect_version", {}),
            ("press.draft",          {}),
        ),
        tags=("prd", "draft"),
    ),

    "publish_for_review": PressStandard(
        name="publish_for_review",
        description=(
            "Full publication path: draft + upload + emit "
            "publishing.draft_ready event so HERALD routes the "
            "review-ready notification to configured recipients."
        ),
        skills=(
            ("press.detect_version", {}),
            ("press.publish",        {"draft": True}),
        ),
        tags=("publish", "review"),
    ),

    "regenerate_versioned": PressStandard(
        name="regenerate_versioned",
        description=(
            "Inspect collision state, preview the next filename, then "
            "draft. Useful when an operator wants to see what name "
            "PRESS will pick before committing to a regenerate."
        ),
        skills=(
            ("press.next_filename", {}),
            ("press.detect_version", {}),
            ("press.draft",          {}),
        ),
        tags=("draft", "preview"),
    ),
}


def list_standards() -> list[PressStandard]:
    """Return all registered standards sorted by name."""
    return sorted(STANDARDS.values(), key=lambda s: s.name)


def get_standard(name: str) -> PressStandard | None:
    """Look up a standard by name; ``None`` if unknown."""
    return STANDARDS.get(name)


__all__ = [
    "PressStandard",
    "STANDARDS",
    "get_standard",
    "list_standards",
]
