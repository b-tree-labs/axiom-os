# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``charts.suggest`` and ``charts.render``: one capability each, every surface.

The CLI (``axi charts``), MCP, the chat tool loop and ``POST /api/v1/charts/*``
all call these, so a chart suggested in a terminal and one suggested in a chat
answer are the same suggestion.
"""

from __future__ import annotations

import json
from typing import Any

from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec, default_registry


def _json(raw: Any) -> Any:
    """A structure from any transport: as given, or JSON text (MCP strings)."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def suggest(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from axiom.extensions.builtins.scidisplay import suggest as sg

    from .. import block

    result = _json(params.get("result"))
    ref = params.get("data_ref")
    if result is None and ref:
        held = block.fetch(str(ref))
        if held is None:
            return SkillResult(ok=False, errors=[f"no data held under {ref!r}; it may have expired"])
        result = held
    if result is None:
        return SkillResult(ok=False, errors=["'result' is required: series, rows, a value or a hierarchy"])
    prefer = _json(params.get("prefer")) or []
    if isinstance(prefer, str):
        prefer = [p.strip() for p in prefer.split(",") if p.strip()]
    try:
        width = int(params.get("width") or sg.DEFAULT_WIDTH)
    except (TypeError, ValueError):
        width = sg.DEFAULT_WIDTH
    try:
        out = block.build(
            result,
            str(params.get("question") or ""),
            title=str(params.get("title") or ""),
            prefer=list(prefer),
            reference=_json(params.get("reference")) or None,
            source=_json(params.get("source")) or None,
            width=width,
            prerender=bool(params.get("prerender", True)),
        )
    except sg.SuggestError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    if out is None:
        quiet = sg.suggest(result, str(params.get("question") or "")).quiet
        return SkillResult(value={"kind": "chart_suggestions", "suggestions": [], "more": [], "quiet": quiet})
    return SkillResult(value=out)


def render(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from axiom.extensions.builtins.scidisplay.chart_kinds import render_spec
    from axiom.extensions.builtins.scidisplay.unified_spec import UnifiedSpecError

    spec = _json(params.get("spec"))
    if not isinstance(spec, dict):
        return SkillResult(ok=False, errors=["'spec' is required: a ChartSpec 2.0 document"])
    try:
        drawn = render_spec(
            spec,
            state=_json(params.get("state")) or None,
            width=int(params["width"]) if params.get("width") else None,
            height=int(params["height"]) if params.get("height") else None,
        )
    except UnifiedSpecError as exc:
        return SkillResult(ok=False, errors=list(exc.problems) or [str(exc)])
    return SkillResult(value=drawn.as_dict())


_SPECS = (
    SkillSpec(
        name="charts.suggest",
        fn=suggest,
        description=(
            "Which charts a result can honestly be drawn as, ranked for the question: the data's "
            "profile, the top 2-3 chart kinds with a one-line reason each and a ChartSpec 2.0 bound "
            "to the data, every other kind that also fits, and why each other kind does not. "
            "Deterministic; a kind the data cannot support is never offered."
        ),
        inputs={
            "result": "the data: {series:[{name,unit,x,y}]}, {rows:[...]}, {value,unit,min,max,target} "
            "or {name,children:[...]}",
            "question": "what was asked, to rank by its intent (optional)",
            "prefer": "kinds to put first, within those that fit (optional)",
            "reference": "{min,max,target} the value is read against (optional)",
            "title": "the chart's title (optional)",
            "width": "the width it will be drawn at, in px (optional)",
            "data_ref": "a held result, instead of 'result' (optional)",
        },
        side_effects=False,
        idempotent=True,
        risk="read",
        cost_class="lookup",
        surfaces=("cli", "mcp", "agent_tool", "skill_md"),
    ),
    SkillSpec(
        name="charts.render",
        fn=render,
        description=(
            "Draw a ChartSpec 2.0 with the platform renderer: SVG, the controls' resolved state, "
            "and notes. The one renderer every surface draws with."
        ),
        inputs={
            "spec": "a ChartSpec 2.0 document with inline data",
            "state": "control values by id (optional)",
            "width": "px (optional)",
            "height": "px (optional)",
        },
        side_effects=False,
        idempotent=True,
        risk="read",
        cost_class="lookup",
        surfaces=("cli", "mcp", "skill_md"),
    ),
)


def bind(registry: SkillRegistry) -> list[str]:
    for spec in _SPECS:
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=False)
    return [s.name for s in _SPECS]


register = bind


def bind_default() -> SkillRegistry:
    registry = default_registry()
    bind(registry)
    return registry


__all__ = ["bind", "bind_default", "register", "render", "suggest"]
