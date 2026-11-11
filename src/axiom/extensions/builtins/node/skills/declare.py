# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``node.declare``: tell KEEL about what Axiom did not install."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import declarations
from ._handoff import next_step
from .inventory import _state_dir


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    kind = str(params.get("kind") or "").strip()
    fields = {k: v for k, v in params.items() if k != "kind" and v not in (None, "")}
    try:
        d, what = declarations.declare(_state_dir(ctx), kind, **fields)
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(
        ok=True,
        value={
            "what": what,
            "declarations": d.as_dict(),
            "file": str(declarations.path_for(_state_dir(ctx))),
        },
        actions_taken=[what],
        next=[next_step("node.inventory", "see the declaration in place")],
    )


__all__ = ["run"]
