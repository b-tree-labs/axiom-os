# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Axi's orchestration tools — exposed to the agent-mode tool loop so Axi maps
natural language to actions instead of any hardcoded keyword routing.

Axi calls these from NL ("how's that calc going?" → ``agent_task_status``);
the LLM phrases the reply itself (no canned copy). The control plane operates on
the *owner's* work — so ``requester``/``principal`` come from ambient context set
by the channel runner per message, never from LLM-supplied params (an LLM must
not be able to assert who it's acting as). Stopping is ownership-gated.

Delegation (``delegate_to_agent``) routes to a subject-matter-expert agent via
the addressee router; as SME agents gain skills, Axi learns to hand off to them.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from ..tools import ActionCategory, ToolDef

# Ambient per-message context, set by the channel runner (not the LLM).
_OP_CTX: ContextVar[dict] = ContextVar("axi_op_ctx", default={})


def set_op_context(
    *, principal: str, requester: str, owner: str | None = None, runner: Any | None = None
) -> None:
    """Bind the current turn's actors. ``principal`` = the agent identity Axi acts
    as; ``requester`` = the human who sent this message; ``owner`` = the principal
    who owns the agent's work (the ownership master controls/stops it). ``owner``
    defaults to ``requester`` (single-owner channel) — set it explicitly to gate
    against a fixed owner so a different requester can't control the work."""
    _OP_CTX.set({"principal": principal, "requester": requester,
                 "owner": owner or requester, "runner": runner})


def _ctx() -> dict:
    return _OP_CTX.get() or {}


TOOLS = [
    ToolDef(
        name="agent_task_status",
        description="List the long-running tasks the agent is currently running for its owner, "
        "with their status and latest progress. Use when the user asks what you're working on "
        "or how a task/calculation is going.",
        category=ActionCategory.READ,
        parameters={"type": "object", "properties": {}, "required": []},
    ),
    ToolDef(
        name="agent_stop_task",
        description="Stop/cancel one of the owner's running tasks by its id. Use when the user "
        "asks to stop, halt, or cancel work. The agent already knows the running task ids from "
        "agent_task_status.",
        category=ActionCategory.WRITE,
        parameters={
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "the task id to stop"}},
            "required": ["task_id"],
        },
    ),
    ToolDef(
        name="verify_prediction",
        description="Record a human-supplied measured value against a digital-twin prediction and "
        "report whether it falls within tolerance. Use when the user reports a measured value to "
        "verify against a prediction discussed in the conversation.",
        category=ActionCategory.WRITE,
        parameters={
            "type": "object",
            "properties": {
                "measured": {"type": "number"},
                "predicted": {"type": "number"},
                "tolerance": {"type": "number"},
                "unit": {"type": "string"},
            },
            "required": ["measured", "predicted"],
        },
    ),
    ToolDef(
        name="delegate_to_agent",
        description="Hand a request to a subject-matter-expert agent (e.g. tidy, triage) by name. "
        "Use when the request is squarely another agent's specialty rather than your own.",
        category=ActionCategory.WRITE,
        parameters={
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "the SME agent, e.g. 'tidy' or 'triage'"},
                "request": {"type": "string", "description": "what to ask that agent to do"},
            },
            "required": ["agent", "request"],
        },
    ),
]


def execute(name: str, params: dict) -> dict:
    ctx = _ctx()
    principal = ctx.get("principal", "@axi:local")
    requester = ctx.get("requester", "unknown")
    owner = ctx.get("owner", requester)
    runner = ctx.get("runner")

    if name == "agent_task_status":
        from axiom.extensions.builtins.connect.agent_control import status_for
        from axiom.extensions.builtins.connect.agent_work import work_status
        from axiom.memory.ownership import new_ownership

        st = status_for(principal, requester, ownership=new_ownership(owner), runner=runner)
        tasks = st["tasks"]
        for t in tasks:
            if t["status"] == "running":
                t["latest"] = (work_status(t["task_id"], runner=runner)["tail"].splitlines() or [""])[-1]
        return {"tasks": tasks}

    if name == "agent_stop_task":
        from axiom.extensions.builtins.connect.agent_control import NotAuthorized, stop_work
        from axiom.memory.ownership import new_ownership

        try:
            r = stop_work(params["task_id"], requester, ownership=new_ownership(owner), runner=runner)
            return {"stopped": r["task_id"], "status": r["status"]}
        except NotAuthorized as exc:
            return {"error": f"not authorized: {exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    if name == "verify_prediction":
        measured = float(params["measured"])
        predicted = float(params["predicted"])
        tol = params.get("tolerance")
        within = None if tol is None else abs(measured - predicted) <= float(tol)
        return {"measured": measured, "predicted": predicted, "unit": params.get("unit", ""),
                "tolerance": tol, "within_tolerance": within}

    if name == "delegate_to_agent":
        from axiom.extensions.builtins.connect.agent_router import discover_agents, resolve_agent, suggest

        agents = discover_agents()
        key = (params.get("agent") or "").lower()
        if key not in agents:
            return {"error": f"unknown agent {key!r}", "did_you_mean": suggest(key, set(agents))}
        r = resolve_agent(key, agents)
        # SME skills are added per agent over time; report the handoff + whether
        # that agent has callable skills yet.
        return {"delegated_to": r.spec.display, "request": params.get("request", ""),
                "has_skills": bool(r.tools)}

    return {"error": f"unknown tool: {name}"}


__all__ = ["TOOLS", "execute", "set_op_context"]
