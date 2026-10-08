# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-056 skill wrapper for ``right_size_pr``.

Exposes the recommendation engine through the platform SkillRegistry +
the MCP tool surface. The shape ``(params, ctx) -> SkillResult`` is the
ADR-056 contract; the underlying logic lives in ``core.recommend``.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from axiom.extensions.builtins.release.right_size.core import (
    ProposedChange,
    RightSizeContext,
    recommend,
)
from axiom.extensions.builtins.release.right_size.providers import (
    detect_provider,
)


def right_size_pr(
    params: dict[str, Any], ctx: SkillContext
) -> SkillResult:
    """Skill entrypoint.

    Expected params:
      - repo (str): e.g. "b-tree-labs/axiom-os"
      - branch_name (str): the would-be PR's head branch
      - files (list[str]): paths the change touches
      - intent (str): one-line description
      - additions (int, optional)
      - deletions (int, optional)
      - author (str, optional): defaults to "@me" via the provider
      - provider (str, optional): override; default = auto-detect
    """
    required = ("repo", "branch_name", "files", "intent")
    missing = [k for k in required if k not in params]
    if missing:
        return SkillResult(
            ok=False,
            errors=[f"missing required params: {', '.join(missing)}"],
        )

    change = ProposedChange(
        branch_name=str(params["branch_name"]),
        files=tuple(params["files"]),
        intent=str(params["intent"]),
        additions=int(params.get("additions", 0)),
        deletions=int(params.get("deletions", 0)),
    )

    provider = detect_provider(explicit=params.get("provider"))
    rs_ctx = RightSizeContext(
        provider=provider,
        repo=str(params["repo"]),
        author=params.get("author"),
    )

    rec = recommend(change, rs_ctx)
    return SkillResult(
        ok=True,
        value={
            "kind": rec.kind,
            "rationale": rec.rationale,
            "target_pr": rec.target_pr,
            "target_pr_url": rec.target_pr_url,
            "cost_estimate_minutes": rec.cost_estimate_minutes,
            "overlap": {str(k): list(v) for k, v in rec.overlap.items()},
        },
    )


__all__ = ["right_size_pr"]
