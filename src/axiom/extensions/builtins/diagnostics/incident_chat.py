# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""AXI-backed conversational responder for the Human<>Agent incident loop.

The structured ``incident_interview`` answers the canonical reviewer questions
but nothing off-script. This responder lets a human talk to the agent about
*anything* in-thread by routing free-text through AXI (the chat agent), seeded
once with the incident context so AXI knows what's going on. It runs in
``ask`` mode (single completion, no tools) so conversation never trips the
remediation gates — applying the fix stays behind the explicit approval.

It degrades gracefully: with no LLM configured (or any turn error) it falls
back to the structured interview, so the loop still works fully offline.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .incident_interview import make_responder as _make_interview

Responder = Callable[[str, dict], str]


def _incident_preamble(ctx: dict[str, Any]) -> str:
    """A compact context seed so AXI can speak to the live incident."""
    title = ctx.get("title") or ctx.get("finding") or "an active incident"
    bits = [
        "You are the on-call Axiom agent in a sysadmin chat channel. "
        f"You are discussing {title}. Answer the operator's questions "
        "conversationally and concretely; you may discuss anything they ask. "
        "A reversible remediation is already staged and awaiting their explicit "
        "approval — never claim to have applied it yourself.",
    ]
    for k in ("pod", "process", "reason", "restarts", "oom"):
        if ctx.get(k) is not None:
            bits.append(f"{k}={ctx[k]}")
    plan = ctx.get("remediation_plan") or {}
    if plan:
        bits.append(f"staged_plan={plan}")
    return " ".join(bits)


def make_axi_responder(
    *,
    agent: Any | None = None,
    fallback: Responder | None = None,
    ask_only: bool = True,
) -> Responder:
    """Build a responder that talks via AXI, falling back to the interview.

    ``agent`` injects a chat agent (a fake in tests); otherwise a ``ChatAgent``
    is built lazily on first use. ``fallback`` overrides the structured
    interview. ``ask_only`` keeps AXI in no-tool completion mode (the safe
    default for a comms channel).
    """
    interview = fallback or _make_interview()
    if agent is not None and ask_only:
        try:
            agent.set_interaction_mode("ask")
        except Exception:
            pass
    state: dict[str, Any] = {"agent": agent, "seeded": False, "tried": agent is not None}

    def _get_agent() -> Any | None:
        if not state["tried"]:
            state["tried"] = True
            try:
                from axiom.extensions.builtins.chat.agent import ChatAgent

                a = ChatAgent()
                if ask_only:
                    a.set_interaction_mode("ask")
                state["agent"] = a
            except Exception:
                state["agent"] = None
        return state["agent"] or None

    def _responder(question: str, ctx: dict) -> str:
        a = _get_agent()
        if a is None:
            return interview(question, ctx)
        msg = question
        if not state["seeded"]:
            msg = _incident_preamble(ctx) + "\n\n" + question
            state["seeded"] = True
        try:
            reply = a.turn(msg, stream=False, raw=True)
        except Exception:
            return interview(question, ctx)
        return reply or interview(question, ctx)

    return _responder


__all__ = ["make_axi_responder"]
