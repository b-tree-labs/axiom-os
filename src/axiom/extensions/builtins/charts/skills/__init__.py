# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``charts.suggest``, ``charts.render`` and ``charts.saved``: one capability each, every surface.

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
            resolve=_figure_source(ctx),
        )
    except UnifiedSpecError as exc:
        return SkillResult(ok=False, errors=list(exc.problems) or [str(exc)])
    return SkillResult(value=drawn.as_dict())


def _state_dir(ctx: SkillContext | None):
    from pathlib import Path

    got = getattr(ctx, "state_dir", None)
    if got:
        return Path(got)
    from axiom.infra.paths import get_user_state_dir

    return Path(get_user_state_dir())


def _figure_source(ctx: SkillContext | None):
    """The figure source for a spec whose data names a served verb: the chart-kinds group's."""
    from ..kinds_group import resolver

    return resolver(_state_dir(ctx))


def kinds_group(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """One chart of every kind from a site's own gold: registered as a prebuilt group for the node.

    ``config`` registers a site's chart-kinds config (a TOML file the site ships); without it the
    one already registered for ``site`` is used. A registered config IS the group: every account
    with access to the node finds it under Prebuilt on the Charts page (``charts.prebuilt_groups``),
    with nothing saved for anybody. ``copy`` additionally saves a personal copy as a group on the
    invocation's principal's own page (never a parameter, as for ``charts.saved``).
    """
    from datetime import date

    from .. import kinds_group as kg

    site = str(params.get("site") or "").strip()
    state_dir = _state_dir(ctx)
    try:
        if params.get("config"):
            cfg = kg.KindsConfig.load(str(params["config"]))
            if site and cfg.site != site:
                return SkillResult(ok=False, errors=[f"the config is for {cfg.site!r}, not {site!r}"])
            kg.register(cfg, state_dir)
            kg.clear_cache()
        else:
            cfg = kg.registered(site, state_dir) if site else None
            if cfg is None:
                return SkillResult(ok=False, errors=[
                    f"no chart-kinds config is registered for {site or 'that site'}; pass config (the site's "
                    "kinds-group TOML) once"])
        replay = date.fromisoformat(str(params["replay"])) if params.get("replay") else None
        group = kg.build_group(cfg, replay=replay)
    except (kg.KindsError, ValueError) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    summary = {
        "site": cfg.site,
        "group": group["name"],
        "replay": (replay or cfg.replay).isoformat(),
        "kinds": [g["kind"] for g in group["graphs"]],
        "sample": [g["kind"] for g in group["graphs"] if (g["spec"].get("catalog") or {}).get("notes") == kg.SAMPLE],
        "origins": {g["kind"]: (g["spec"].get("catalog") or {}).get("origin", "") for g in group["graphs"]},
        "ids": {g["kind"]: g["spec"]["id"] for g in group["graphs"]},
    }
    if params.get("dry_run"):
        return SkillResult(value={**summary, "saved": False, "group_document": group})
    if not params.get("copy"):
        return SkillResult(value={**summary, "saved": False, "shared": True})
    principal = getattr(getattr(ctx, "principal", None), "handle", "") or ""
    if not principal:
        return SkillResult(ok=False, errors=["a saved group belongs to somebody; this call has no principal"])
    from axiom.extensions.builtins.webapp.views import read_view, write_view

    from ..saved import GROUPS_SURFACE

    write_view(principal, GROUPS_SURFACE, kg.merge_into(read_view(principal, GROUPS_SURFACE), group))
    return SkillResult(value={**summary, "saved": True, "shared": True, "principal": principal})


def prebuilt_groups(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """The groups this node provides to everyone who may use its Charts page (same for every account)."""
    from .. import prebuilt

    principal = getattr(getattr(ctx, "principal", None), "handle", "") or ""
    if not principal:
        return SkillResult(ok=False, errors=["prebuilt groups are for somebody signed in; this call has no principal"])
    return SkillResult(value={"prebuilt": prebuilt.listing(_state_dir(ctx))})


def saved(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Chart's catalog (MVP-03-A2): the caller's saved charts, or one drawn by its id.

    Whose charts is the invocation's principal, never a parameter: a caller that could
    name the owner could read anybody's.
    """
    from .. import saved as catalog

    principal = getattr(getattr(ctx, "principal", None), "handle", "") or ""
    if not principal:
        return SkillResult(ok=False, errors=["saved charts belong to somebody; this call has no principal"])
    chart_id = str(params.get("id") or "").strip()
    if not chart_id:
        return SkillResult(value={"charts": catalog.listing(principal)})
    spec = catalog.spec_for(principal, chart_id)
    if spec is None:
        return SkillResult(ok=False, errors=[f"no saved chart {chart_id!r}; list them with charts.saved"])
    drawn = render({"spec": spec, **{k: params[k] for k in ("width", "height") if params.get(k)}}, ctx)
    if not drawn.ok:
        return drawn
    return SkillResult(value={"id": chart_id, "spec": spec, **drawn.value})


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
    SkillSpec(
        name="charts.saved",
        fn=saved,
        description=(
            "Chart's catalog: the charts you saved (from the Charts page or a chat answer), each "
            "by its stable catalog id; with an id, that chart drawn by the platform renderer, the "
            "same on the page, the CLI and MCP."
        ),
        inputs={
            "id": "a saved chart's catalog id, to draw it (optional; omit to list)",
            "width": "px (optional)",
            "height": "px (optional)",
        },
        side_effects=False,
        idempotent=True,
        risk="read",
        cost_class="lookup",
        surfaces=("cli", "mcp", "agent_tool", "skill_md"),
    ),
)


_KINDS_SPEC = SkillSpec(
    name="charts.kinds_group",
    fn=kinds_group,
    description=(
        "Show me every chart kind: one chart of every ChartSpec 2.0 kind drawn from a site's own gold "
        "(lines, tiles, gauge, state, pie, scatter, heatmap, flame of the platform's own timing, 3D), "
        "with a looping replay of a recorded day, offered to every account as a prebuilt group on the Charts page. Each chart reads "
        "gold at draw time; a kind with no real source is labelled SAMPLE."
    ),
    inputs={
        "site": "the site whose gold to draw",
        "replay": "the recorded day to replay, YYYY-MM-DD (optional; the site's default; at least a day old)",
        "config": "the site's chart-kinds TOML, to register it (optional once registered)",
        "dry_run": "build the group and say what it would offer, without registering anything more (optional)",
        "copy": "also save a personal copy on the caller's own Charts page (optional; the registered "
        "group is already shared with every account)",
    },
    side_effects=True,
    idempotent=True,
    risk="reversible-write",
    # Nothing to have done first: a config is registered by the same call that builds the group.
    requires=(),
    cost_class="lookup",
    surfaces=("cli", "mcp", "agent_tool", "skill_md"),
)


_PREBUILT_SPEC = SkillSpec(
    name="charts.prebuilt_groups",
    fn=prebuilt_groups,
    description=(
        "The prebuilt groups this node provides on the Charts page: shared by every account with access, "
        "never copied into anybody's saved views; open one like any group."
    ),
    inputs={},
    side_effects=False,
    idempotent=True,
    risk="read",
    cost_class="lookup",
    surfaces=("cli", "mcp", "agent_tool", "skill_md"),
)


def bind(registry: SkillRegistry) -> list[str]:
    for spec in _SPECS:
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=False)
    if not registry.has(_KINDS_SPEC.name):
        registry.register_skill(_KINDS_SPEC, mutating=True)
    if not registry.has(_PREBUILT_SPEC.name):
        registry.register_skill(_PREBUILT_SPEC, mutating=False)
    return [s.name for s in (*_SPECS, _KINDS_SPEC, _PREBUILT_SPEC)]


register = bind


def bind_default() -> SkillRegistry:
    registry = default_registry()
    bind(registry)
    return registry


__all__ = ["bind", "bind_default", "kinds_group", "prebuilt_groups", "register", "render", "suggest"]
