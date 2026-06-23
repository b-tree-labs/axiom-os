# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Phone control plane: query / stop / redirect an agent's work (ADR-074, C3).

From any channel (especially SMS on a phone), the *owner* can ask what their
agents are working on, stop a run, or redirect it — gated by the ownership model
(ADR-026): CONTROL to stop, EFFORT to redirect. This wires ``Ownership`` to
agents for the first time.

- **status** — the owner's tasks + progress (reads `agent_work`).
- **stop**   — cancel a subprocess task (`TaskRunner.cancel`) and/or post a STOP
               interrupt for an in-flight agent run.
- **redirect** — deliver a new instruction via an `InterruptInbox` a run loop
               polls (implements the previously-unwired
               ``InterruptPolicy.USER_SIGNAL_ONLY`` delivery).

All mutating verbs are ownership-gated; unauthorized requesters are refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from axiom.memory.ownership import Ownership, Right, can_exercise

from .agent_work import list_work, work_status


def _now() -> str:
    return datetime.now(UTC).isoformat()


class NotAuthorized(PermissionError):
    """Raised when a requester lacks the ownership right for a control verb."""


def _require(ownership: Ownership, requester: str, right: Right, at: str | None) -> None:
    if not can_exercise(ownership, requester, right, at or _now()):
        raise NotAuthorized(f"{requester} lacks {right.value} over this agent's work")


# --- redirect/stop delivery to in-flight runs ---------------------------------

@dataclass
class Interrupt:
    kind: str          # "stop" | "redirect"
    payload: str = ""  # redirect instruction
    at: str = field(default_factory=_now)


class InterruptInbox:
    """Per-run interrupt queue. A run loop polls ``drain(run_id)`` between steps
    and acts on STOP/REDIRECT — the delivery mechanism ``InterruptPolicy``
    declared but never wired."""

    def __init__(self) -> None:
        self._queues: dict[str, list[Interrupt]] = {}

    def post(self, run_id: str, interrupt: Interrupt) -> None:
        self._queues.setdefault(run_id, []).append(interrupt)

    def peek(self, run_id: str) -> list[Interrupt]:
        return list(self._queues.get(run_id, ()))

    def drain(self, run_id: str) -> list[Interrupt]:
        return self._queues.pop(run_id, [])


# --- control verbs ------------------------------------------------------------

def status_for(
    principal: str,
    requester: str,
    *,
    ownership: Ownership,
    runner: Any | None = None,
    task_id: str | None = None,
    at: str | None = None,
) -> dict:
    """What is ``principal`` working on? (or detail one task). Requester must
    hold CONTROL or EFFORT — you can see the work you can govern."""
    if not (
        can_exercise(ownership, requester, Right.CONTROL, at or _now())
        or can_exercise(ownership, requester, Right.EFFORT, at or _now())
    ):
        raise NotAuthorized(f"{requester} may not view this agent's work")
    if task_id:
        return work_status(task_id, runner=runner)
    return {"principal": principal, "tasks": list_work(principal, runner=runner)}


def stop_work(
    task_id: str,
    requester: str,
    *,
    ownership: Ownership,
    runner: Any | None = None,
    run_id: str | None = None,
    inbox: InterruptInbox | None = None,
    at: str | None = None,
) -> dict:
    """Stop a task (CONTROL). Cancels the subprocess and, if an in-flight agent
    run is named, posts a STOP interrupt for the run loop to honor."""
    _require(ownership, requester, Right.CONTROL, at)
    from .agent_work import _runner as _resolve_runner

    r = _resolve_runner(runner)
    task = r.cancel(task_id)
    if run_id and inbox is not None:
        inbox.post(run_id, Interrupt(kind="stop"))
    return {"task_id": task_id, "status": task.status, "stopped_by": requester}


def redirect_work(
    run_id: str,
    requester: str,
    instruction: str,
    *,
    ownership: Ownership,
    inbox: InterruptInbox,
    at: str | None = None,
) -> dict:
    """Redirect an in-flight run with a new instruction (EFFORT). Delivered via
    the interrupt inbox the run loop polls."""
    _require(ownership, requester, Right.EFFORT, at)
    inbox.post(run_id, Interrupt(kind="redirect", payload=instruction))
    return {"run_id": run_id, "redirected_by": requester, "instruction": instruction}


__all__ = [
    "NotAuthorized",
    "Interrupt",
    "InterruptInbox",
    "status_for",
    "stop_work",
    "redirect_work",
]
