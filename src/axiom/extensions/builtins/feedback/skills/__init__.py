# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The feedback skills (ADR-056): one each for the CLI, MCP, chat and HTTP (DC-16).

* ``feedback.report``: good or bad and an optional sentence; the server captures the rest
  (LOOP-B1, B2). An agent names the ``capability`` it called to report that call (B3).
* ``feedback.decide``: the owner's answer to the ask (B5): the chosen rollback, or fix
  forward with a reason.
* ``feedback.reports``: one flag's reports, grouped by cause (B6).

The reporter is always the caller (``ctx.actor``, else the context's principal), never a
parameter. The subject a web report resolves flags for comes from the session: ``_subject``
and ``_roles`` are read only on the web surface, which the dispatch chokepoint stamps.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skill_declarations import NextStep
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec, default_registry

from .. import service

_SURFACES = ("cli", "mcp", "agent_tool", "skill_md")


def _caller(ctx: SkillContext | None) -> str:
    if ctx is None:
        return ""
    return str(getattr(ctx, "actor", None) or getattr(getattr(ctx, "principal", None), "handle", "") or "")


def _web(ctx: SkillContext | None) -> bool:
    return getattr(ctx, "surface", None) == "web"


def report(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    if not service.is_on(service.REPORTS_FLAG):
        return SkillResult(ok=False, errors=[
            f"feedback is off on this node; a site admin turns it on with the {service.REPORTS_FLAG} flag"])
    subject = params.get("_subject") if _web(ctx) and isinstance(params.get("_subject"), dict) else None
    try:
        out = service.report(params, reporter=_caller(ctx), surface=getattr(ctx, "surface", None),
                             subject=subject)
    except service.Refused as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    g = out["group"]
    said = f"thanks: recorded with {service._count(g['bad'] + g['good'], 'report')} of {service.cause_label(g)}"
    actions = [said]
    if out["auto_stop"] and out["auto_stop"]["stopped"]:
        actions.append(f"stopped {g['flag']}: {out['auto_stop']['reason']}")
    return SkillResult(value=out, actions_taken=actions)


def decide(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    roles = tuple(str(r) for r in (params.get("_roles") or ())) if _web(ctx) else ()
    try:
        out = service.decide(str(params.get("group") or "").strip(), str(params.get("choice") or "").strip(),
                             caller=_caller(ctx), reason=str(params.get("reason") or ""), roles=roles)
    except service.Refused as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(value=out, actions_taken=[f"{service.LABELS[out['choice']]}: {out['group']['status']}"])


def reports(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    key = str(params.get("flag") or "").strip()
    if not key:
        return SkillResult(ok=False, errors=["name the flag: flag is required"])
    try:
        limit = max(1, min(int(params.get("limit") or 20), 100))
    except (TypeError, ValueError):
        limit = 20
    return SkillResult(value=service.for_flag(key, limit=limit))


SPECS = (
    SkillSpec(
        name="feedback.report",
        fn=report,
        description=(
            "Report that something is good or bad, in one action, with an optional sentence. The server "
            "captures the surface, release, node, the flags on for you and the capability or screen. An "
            "agent reports a call that failed, was refused or returned something it could not use by "
            "naming that capability (and outcome=unusable when the call succeeded)."
        ),
        inputs={
            "verdict": "good or bad (required)",
            "text": "optional: one sentence",
            "severe": "optional: true marks a bad report severe",
            "capability": "optional: the capability call this is about (an agent's own call)",
            "outcome": "optional: failed, refused or unusable (an agent's call)",
            "flag": "optional: the flag it is about, when you know; it must be on for you",
        },
        side_effects=True,
        idempotent=False,
        surfaces=_SURFACES,
        risk="reversible-write",
        requires=(),
        next_steps=(NextStep("feedback.reports", "ok", "see the reports grouped with it, by flag"),),
    ),
    SkillSpec(
        name="feedback.decide",
        fn=decide,
        description=(
            "Answer a feedback ask you own: stop_flag (the flag's kill switch), roll_back_slot (the previous "
            "slot; offered, the switch is run on the node), or fix_forward with a reason."
        ),
        inputs={
            "group": "the feedback group id (required)",
            "choice": "stop_flag, roll_back_slot or fix_forward (required)",
            "reason": "why; required to fix forward",
        },
        side_effects=True,
        idempotent=False,
        surfaces=_SURFACES,
        risk="reversible-write",
        requires=(),
        next_steps=(NextStep("feedback.reports", "ok", "see the flag's reports"),),
    ),
    SkillSpec(
        name="feedback.reports",
        fn=reports,
        description="One flag's feedback reports, grouped by cause, with counts, distinct reporters and status.",
        inputs={"flag": "the flag key (required)", "limit": "optional: most groups (default 20)"},
        side_effects=False,
        idempotent=True,
        surfaces=_SURFACES,
        risk="read",
        cost_class="lookup",
    ),
)


def bind(registry: SkillRegistry) -> list[str]:
    names = []
    for spec in SPECS:
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=spec.risk != "read")
        names.append(spec.name)
    return names


register = bind


def bind_default() -> SkillRegistry:
    registry = default_registry()
    bind(registry)
    return registry


__all__ = ["SPECS", "bind", "bind_default", "decide", "register", "report", "reports"]
