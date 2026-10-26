# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``sites.checklist``: the ADR-178 readiness checklist for one site. Reads only."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from .. import checklist as _checklist

    site = str(params.get("site") or "").strip()
    if not site:
        return SkillResult(ok=False, errors=["missing required param: site"])
    kw: dict[str, Any] = {"min_version": str(params.get("min_version") or "")}
    if params.get("stopped_after_h"):
        kw["stopped_after_s"] = float(params["stopped_after_h"]) * 3600
    try:
        doc = _checklist.report(site, **kw)
    except LookupError:
        return SkillResult(ok=False, errors=[f"no such site: {site}"])
    return SkillResult(ok=True, value={**doc, "text": _checklist.render(doc)})
