# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``press.standards`` — list the registered PRESS standards bundles."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from axiom.extensions.builtins.publishing.standards import list_standards

    category = params.get("category")
    items = []
    for std in list_standards():
        if category and std.category != category:
            continue
        items.append({
            "name":        std.name,
            "description": std.description,
            "category":    std.category,
            "version":     std.version,
            "tags":        list(std.tags),
            "skills":      [s[0] for s in std.skills],
        })
    return SkillResult(
        ok=True,
        value={"resource": "press_standards", "count": len(items), "items": items},
    )


__all__ = ["run"]
