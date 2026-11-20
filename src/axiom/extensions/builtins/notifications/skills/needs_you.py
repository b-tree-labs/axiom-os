# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``notifications.needs_you`` — what needs me: the caller's own "Needs you" queue.

The person is the caller (``ctx.actor``), never a parameter: nobody reads
another person's queue through this (MVP-21-A1). Read-only.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from axiom.infra.audit_trail import DEFAULT_PRINCIPAL

    from ..needs_you import needs_you

    person = getattr(ctx, "actor", None) or DEFAULT_PRINCIPAL
    try:
        limit = max(1, min(int(params.get("limit") or 50), 200))
    except (TypeError, ValueError):
        return SkillResult(ok=False, errors=["limit is a whole number from 1 to 200"])
    approvals = None
    if getattr(ctx, "state_dir", None):
        from pathlib import Path

        candidate = Path(ctx.state_dir) / "orchestrator" / "approvals.json"
        approvals = candidate if candidate.exists() else None
    return SkillResult(value=needs_you(person, approvals_path=approvals, limit=limit))


__all__ = ["run"]
