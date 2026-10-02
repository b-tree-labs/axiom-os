# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``press.next_filename`` — preview Finder-style non-clobbering name."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    target = params.get("target")
    if not target:
        return SkillResult(ok=False, errors=["missing required param: target"])
    from axiom.extensions.builtins.publishing.engine import _non_clobbering_path

    chosen = _non_clobbering_path(Path(target))
    return SkillResult(
        ok=True,
        value={
            "target": str(target),
            "next": str(chosen),
            "would_collide": str(chosen) != str(target),
        },
    )


__all__ = ["run"]
