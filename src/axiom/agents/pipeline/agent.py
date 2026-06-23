# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Agent run data shapes + AgentPipeline shell — ADR-034 §D3.

An agent run is a sequence of MemoryFragments bound by ``run_id``. This module
defines the in-memory Python shape; the bridge to MemoryFragment lands once
ADR-035's accountable-human plumbing settles. Per ADR-034: every event is a
memory write; replay is replay of memory, not stdout.

Stage 1 surface (this module):
- AgentEvent / AgentRun / AgentRunRequest dataclasses (frozen).
- AgentEventKind / AgentRunStatus / InterruptPolicy enums.
- AgentPipeline with an injected step_fn — full integration with PlanPipeline,
  AEOSToolRuntime, sandbox, and CompositionService follows in Stage 2.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Event shape
# ---------------------------------------------------------------------------


class AgentEventKind(str, Enum):
    RUN_STARTED = "run_started"
    STEP_STARTED = "step_started"
    THOUGHT = "thought"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    STEP_COMPLETED = "step_completed"
    INTERRUPT_RECEIVED = "interrupt_received"
    HANDOFF_TO_PEER = "handoff_to_peer"
    RUN_COMPLETED = "run_completed"
    RUN_ABORTED = "run_aborted"


def _gen_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class AgentEvent:
    run_id: str
    kind: AgentEventKind
    event_id: str = field(default_factory=_gen_id)
    step_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Run + request
# ---------------------------------------------------------------------------


class AgentRunStatus(str, Enum):
    INITIALIZING = "initializing"
    RUNNING = "running"
    PAUSED_FOR_APPROVAL = "paused_for_approval"
    COMPLETED = "completed"
    ABORTED = "aborted"
    HANDOFF_TO_PEER = "handoff_to_peer"
    FAILED_PROOF = "failed_proof"


_TERMINAL_STATUSES = frozenset(
    {
        AgentRunStatus.COMPLETED,
        AgentRunStatus.ABORTED,
        AgentRunStatus.HANDOFF_TO_PEER,
        AgentRunStatus.FAILED_PROOF,
    }
)


class InterruptPolicy(str, Enum):
    USER_SIGNAL_ONLY = "user_signal_only"
    NEVER = "never"
    PER_STEP_GATE = "per_step_gate"


@dataclass(frozen=True)
class SandboxSpec:
    """Stage 1 placeholder; full vocabulary lands with the AEOS reach amendment."""

    name: str = "default"
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentRunRequest:
    plan_id: str
    principal_id: str                                  # actor (the agent)
    accountable_human_id: str                          # ADR-035; mandatory
    sandbox: SandboxSpec | None = None
    interrupt_policy: InterruptPolicy = InterruptPolicy.USER_SIGNAL_ONLY
    max_steps: int = 100
    budget_usd: float | None = None


@dataclass(frozen=True)
class AgentRun:
    plan_id: str
    principal_id: str
    accountable_human_id: str
    run_id: str = field(default_factory=_gen_id)
    status: AgentRunStatus = AgentRunStatus.INITIALIZING
    events: tuple[AgentEvent, ...] = ()

    def with_status(self, status: AgentRunStatus) -> AgentRun:
        return replace(self, status=status)

    def append_event(self, event: AgentEvent) -> AgentRun:
        if event.run_id != self.run_id:
            raise ValueError(
                f"run_id mismatch: event.run_id={event.run_id!r} != run.run_id={self.run_id!r}"
            )
        return replace(self, events=self.events + (event,))

    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# Pipeline shell
# ---------------------------------------------------------------------------


# step_fn returns the next event to append, or None to signal "terminate cleanly"
StepFn = Callable[[AgentRun], AgentEvent | None]
OnEventFn = Callable[[AgentEvent], None]


def _noop(_: AgentEvent) -> None:
    pass


@dataclass
class AgentPipeline:
    """Shell pipeline; full execution loop with AEOS tools + sandbox lands in Stage 2."""

    step_fn: StepFn
    on_event: OnEventFn = _noop

    def run(self, request: AgentRunRequest) -> AgentRun:
        run = AgentRun(
            plan_id=request.plan_id,
            principal_id=request.principal_id,
            accountable_human_id=request.accountable_human_id,
            status=AgentRunStatus.RUNNING,
        )
        started = AgentEvent(run_id=run.run_id, kind=AgentEventKind.RUN_STARTED)
        run = run.append_event(started)
        self.on_event(started)

        steps_taken = 0
        while not run.is_terminal():
            if steps_taken >= request.max_steps:
                aborted = AgentEvent(
                    run_id=run.run_id,
                    kind=AgentEventKind.RUN_ABORTED,
                    payload={"reason": "max_steps"},
                )
                run = run.append_event(aborted)
                self.on_event(aborted)
                run = run.with_status(AgentRunStatus.ABORTED)
                return run

            next_event = self.step_fn(run)
            if next_event is None:
                # clean termination
                completed = AgentEvent(
                    run_id=run.run_id, kind=AgentEventKind.RUN_COMPLETED
                )
                run = run.append_event(completed)
                self.on_event(completed)
                run = run.with_status(AgentRunStatus.COMPLETED)
                return run

            run = run.append_event(next_event)
            self.on_event(next_event)
            steps_taken += 1

        return run
