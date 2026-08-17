# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``press.draft`` — render a draft artifact locally (no upload)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    src = params.get("source")
    if not src:
        return SkillResult(ok=False, errors=["missing required param: source"])
    source_path = Path(src).resolve()
    if not source_path.exists():
        return SkillResult(
            ok=False, errors=[f"source file not found: {src}"]
        )

    from axiom.extensions.builtins.publishing.engine import PublisherEngine

    try:
        engine = PublisherEngine()
        output = engine.generate(source_path)
    except Exception as exc:  # noqa: BLE001
        return SkillResult(
            ok=False, errors=[f"{type(exc).__name__}: {exc}"]
        )

    return SkillResult(
        ok=True,
        value={"source": str(source_path), "output": str(output)},
        actions_taken=[f"drafted {source_path.name} → {Path(output).name}"],
    )


__all__ = ["run"]
