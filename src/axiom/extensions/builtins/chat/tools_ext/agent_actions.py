# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Query the agent action-provenance ledger from neut chat (#665).

Lets Neut answer "what did TIDY do yesterday?" / "why was that branch
prune refused?" — a read-only, thin delegate to
``axiom.policy.action_ledger``, the guard-emitted ledger of every
autonomous agent action (refusals included).
"""

from __future__ import annotations

from typing import Any

from axiom.infra.orchestrator.actions import ActionCategory

from ..tools import ToolDef

TOOLS = [
    ToolDef(
        name="agent_actions",
        description=(
            "Query the agent action-provenance ledger — every autonomous "
            "agent action that went through the platform action guard "
            "(branch prunes, artifact cleanups, issue auto-closes, ...), "
            "including refused actions with the refusing rule and "
            "completed actions with their undo handle. Use this when the "
            "user asks what an agent (TIDY, RIVET, PLINTH, ...) did, or "
            "why an action was refused."
        ),
        category=ActionCategory.READ,
        parameters={
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "Filter to one agent (e.g. 'tidy', 'rivet').",
                },
                "op_class": {
                    "type": "string",
                    "description": (
                        "Filter by operation class (e.g. 'git.branch.delete')."
                    ),
                },
                "outcome": {
                    "type": "string",
                    "description": "'proceeded', 'refused', or 'failed'.",
                },
                "since": {
                    "type": "string",
                    "description": (
                        "Window floor: shorthand (Nm/Nh/Nd/Nw, e.g. '24h') "
                        "or ISO-8601."
                    ),
                },
                "until": {
                    "type": "string",
                    "description": "Window ceiling: shorthand or ISO-8601.",
                },
                "text": {
                    "type": "string",
                    "description": (
                        "Substring match against candidate / name / rule / "
                        "undo handle."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum records to return (default 25).",
                },
            },
        },
    ),
]


def execute(name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Execute the agent_actions tool. Called by the chat tool loop."""
    if name != "agent_actions":
        return {"error": f"unknown tool: {name}"}
    from axiom.policy.action_ledger import search_actions

    return search_actions(
        agent=params.get("agent"),
        op_class=params.get("op_class"),
        outcome=params.get("outcome"),
        since=params.get("since"),
        until=params.get("until"),
        text=params.get("text"),
        limit=int(params.get("limit") or 25),
    )
