# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``press.scope_for_source`` — resolve a source doc's filesystem scope."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    src = params.get("source")
    if not src:
        return SkillResult(ok=False, errors=["missing required param: source"])
    from axiom.extensions.builtins.publishing.engine import (
        _find_source_filesystem_scope,
    )

    scope = _find_source_filesystem_scope(Path(src))
    return SkillResult(
        ok=True,
        value={"source": str(src), "scope": str(scope) if scope else None},
    )


__all__ = ["run"]
