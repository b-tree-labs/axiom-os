# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Incident-interview skill — the questions a good reviewer asks, and answers
to them, for the Human<>Agent HITL loop (ADR-074, #540).

This is the agent "knowing the right questions": it proactively surfaces the
canonical questions a reviewer needs answered, and fields free-text questions
in-thread. Live state (the *actual* current value) is pulled through an
injected ``live`` callable so the skill stays transport-agnostic — the caller
binds it to a ``host_exec`` query (kubectl over ssh, an API call, …). With no
``live`` binding it answers from the staged plan.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_GiB = 1024**3
_MiB = 1024**2

# The canonical reviewer checklist — what a human should know before approving.
_CANONICAL = [
    "What is the current state/value?",
    "How often is it happening / what's the blast radius?",
    "Is the fix reversible, and what's the risk?",
    "What's the root cause?",
    "What exactly will you do on approval?",
]


def suggested_questions(context: dict[str, Any]) -> list[str]:
    """The questions the agent proactively invites — the reviewer checklist."""
    return list(_CANONICAL)


def answer(question: str, context: dict[str, Any], *, live: Callable[[], str | None] | None = None) -> str:
    """Answer a reviewer's question. Uses ``live()`` for current-state asks."""
    q = question.lower()
    plan = context.get("remediation_plan", {}) or {}

    if any(w in q for w in ("current", "limit", "value", "state", "memory", "now")):
        if live is not None:
            v = live()
            if v:
                return v
        old = plan.get("old_limit_bytes")
        new = plan.get("new_limit_bytes")
        if old and new:
            return f"Current ~{old // _MiB} MiB; proposed {new // _GiB} GiB."
        return "No live binding available; see the staged plan."

    if any(w in q for w in ("revers", "risk", "safe", "rollback", "undo")):
        rev = plan.get("reversible", False)
        return ("Reversible — the change can be set back with no data impact." if rev
                else "Not cleanly reversible; review carefully before approving.")

    if any(w in q for w in ("often", "restart", "how many", "blast", "impact", "scope")):
        return (f"{context.get('restarts')} occurrences ({context.get('reason', 'failure')}). "
                f"Scope: {context.get('pod') or context.get('process') or 'the affected workload'}.")

    if any(w in q for w in ("root", "why", "cause", "reason")):
        return ("Root cause: a too-low resource limit relative to the working set, so the "
                "kernel OOM-kills the process repeatedly." if context.get("oom")
                else f"Last failure reason: {context.get('reason', 'unknown')}.")

    if any(w in q for w in ("do", "plan", "fix", "apply", "action", "step")):
        new = plan.get("new_limit_bytes")
        tail = f" (raise limit to {new // _GiB} GiB)" if new else ""
        return f"On approval the remediator applies the staged, reversible fix{tail} and verifies recovery."

    return ("I can answer: current state/value, frequency & blast radius, "
            "reversibility & risk, root cause, or exactly what I'll do.")


def make_responder(live: Callable[[], str | None] | None = None) -> Callable[[str, dict], str]:
    """Bind the interview into the IncidentConversation responder shape."""
    def _responder(question: str, ctx: dict) -> str:
        return answer(question, ctx, live=live)

    return _responder


__all__ = ["suggested_questions", "answer", "make_responder"]
