# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``agents.catalog`` / ``activate`` / ``deactivate`` / ``assign``: which agents are on Team.

An agent type is dormant at a site until someone adds it (``activate``); the
person who adds it answers for it. ``deactivate`` returns it to dormant and keeps
its history. ``assign`` changes who answers for an active agent. The record and
its rules are :mod:`axiom.agents.activation`; each write here notes its change,
so the dispatch chokepoint's one audit entry says what changed.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def _who(params: dict[str, Any], ctx: SkillContext | None) -> str:
    return str(params.get("by") or "").strip() or str(getattr(ctx, "actor", "") or "").strip()


def catalog(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from axiom.agents import activation

    del ctx
    types = activation.catalog(params.get("_agents"))
    if params.get("dormant"):
        types = [t for t in types if not t["active"]]
    on = sum(1 for t in types if t["active"])
    return SkillResult(ok=True, value={"types": types},
                       actions_taken=[f"{len(types)} agent types, {on} on Team"])


def _run(fn, *args, **kw) -> SkillResult:
    from axiom.agents.activation import ActivationError

    try:
        value = fn(*args, **kw)
    except ActivationError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(ok=True, value=value)


def activate(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from axiom.agents import activation

    agent = str(params.get("agent") or "").strip()
    if not agent:
        return SkillResult(ok=False, errors=["name the agent type: agent=@handle (agents.catalog lists them)"])
    by = _who(params, ctx)
    if not by:
        return SkillResult(ok=False, errors=["name who is adding it: by=<email>; they answer for it"])
    out = _run(activation.activate, agent, by=by, roster=params.get("_agents"))
    if out.ok:
        out.actions_taken = [f"{out.value['name']} is on Team; {out.value['accountable']} answers for it"]
    return out


def deactivate(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from axiom.agents import activation

    agent = str(params.get("agent") or "").strip()
    if not agent:
        return SkillResult(ok=False, errors=["name the agent: agent=@handle"])
    out = _run(activation.deactivate, agent, by=_who(params, ctx), roster=params.get("_agents"))
    if out.ok:
        out.actions_taken = [f"{out.value['name']} is dormant: off Team, no requests; its history stays"]
    return out


def assign(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from axiom.agents import activation

    agent = str(params.get("agent") or "").strip()
    person = str(params.get("person") or "").strip()
    if not agent or not person:
        return SkillResult(ok=False, errors=["name the agent and the person: agent=@handle person=<email or @handle>"])
    out = _run(activation.assign, agent, person, by=_who(params, ctx), roster=params.get("_agents"))
    if out.ok:
        out.actions_taken = [f"{out.value['accountable']} answers for {out.value['name']}"]
    return out


__all__ = ["activate", "assign", "catalog", "deactivate"]
