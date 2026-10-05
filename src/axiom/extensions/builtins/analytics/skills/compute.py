# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``analytics`` skill — deterministic series math (ADR-113 Tiers 1-3).

One skill, projected to CLI / MCP / agent-tool per ADR-072/073. The model calls
this instead of computing a number inline; the returned result carries a
provenance stamp the output-provenance gate admits.
"""
from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..core import OPS, AnalyticsError, compute


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    p = params or {}
    op = p.get("op")
    if not op:
        return SkillResult(ok=False, errors=[f"'op' is required; one of: {', '.join(OPS)}"])
    if "series" not in p:
        return SkillResult(ok=False, errors=["'series' is required (list, JSON, CSV/table, or inline)"])
    try:
        result = compute(
            op,
            p.get("series"),
            column=p.get("column"),
            params=p.get("params") or {},
            source=p.get("source"),
        )
    except AnalyticsError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(ok=True, value={"resource": "analytics", "result": result})
