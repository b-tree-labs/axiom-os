# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``memory.conflicts_list`` skill — the read-only conflict queue view
(ADR-087 D3, PRD F3).

Kept-both pairs land here when absorb/dedup hits the ambiguous band or
a source's content drifts under a stable ref. P2 exposes the queue
**read-only** — adjudication verbs (accept / merge / dismiss) are a
deliberate knob deferral, so this skill never mutates anything.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def conflicts_list(
    params: dict[str, Any], ctx: SkillContext | None
) -> SkillResult:
    """List queued memory conflicts, oldest first. Read-only."""
    composition = params.get("composition")
    if composition is None:
        return SkillResult(ok=False, errors=["no composition service provided"])

    from axiom.memory.dedup import list_conflicts

    principal = params.get("principal") or None
    conflicts = list_conflicts(composition, principal=principal)
    return SkillResult(
        ok=True,
        value={
            "principal": principal or "",
            "count": len(conflicts),
            "conflicts": conflicts,
        },
    )
