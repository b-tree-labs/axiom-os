# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``RunHandle``: the internal handle for a long or agent-backed call (H1).

ADR-232 part 6 requires a task handle for any capability whose latency class is
``long`` or whose binding is remote; ADR-233 part 3 names the same shape
``RunHandle`` because chat subagents need it too. This is that shape, internal
only. Nothing here speaks MCP tasks or A2A; the poll tools in :mod:`.poll` are
the one external contract (H2).

A handle is an immutable snapshot. State changes produce a new snapshot through
:meth:`RunHandle.advance`, which is the only place the state machine lives.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any

from axiom.infra.identifiers import generate_id

RUN_ID_PREFIX = "run_"


class RunState(str, Enum):
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_NEEDED = "input_needed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in TERMINAL_STATES


TERMINAL_STATES = frozenset({RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED})

#: Legal moves. Terminal states have none: a finished run is never re-run.
TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.SUBMITTED: frozenset({RunState.WORKING, RunState.FAILED, RunState.CANCELLED}),
    RunState.WORKING: frozenset(
        {RunState.INPUT_NEEDED, RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.INPUT_NEEDED: frozenset({RunState.WORKING, RunState.FAILED, RunState.CANCELLED}),
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}


class IllegalTransition(ValueError):
    """A state change the machine does not allow. A programming error, not a surface result."""


def new_run_id() -> str:
    """Generated, never supplied by a caller."""
    return RUN_ID_PREFIX + generate_id()


@dataclass(frozen=True)
class RunHandle:
    id: str
    capability: str
    principal: str
    """``@name:context`` of the caller the run acts for."""
    state: RunState
    created_at: datetime
    updated_at: datetime
    parent_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result_ref: str | None = None
    cause: str | None = None
    """Why a run failed or was cancelled (``budget_exhausted``, ``user_cancelled``, ...)."""
    budget: dict[str, Any] | None = None
    """Granted, drawn, remaining and outcome, from the run's ledger. ``None`` when unbudgeted."""

    @property
    def terminal(self) -> bool:
        return self.state.terminal

    def advance(
        self,
        state: RunState,
        *,
        now: datetime,
        cause: str | None = None,
        result_ref: str | None = None,
    ) -> RunHandle:
        if state not in TRANSITIONS[self.state]:
            raise IllegalTransition(f"{self.state.value} -> {state.value} is not allowed")
        changes: dict[str, Any] = {"state": state, "updated_at": now}
        if state is RunState.WORKING and self.started_at is None:
            changes["started_at"] = now
        if state.terminal:
            changes["finished_at"] = now
        if cause is not None:
            changes["cause"] = cause
        if result_ref is not None:
            changes["result_ref"] = result_ref
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        def iso(d: datetime | None) -> str | None:
            return d.isoformat() if d else None

        return {
            "run_id": self.id,
            "parent_id": self.parent_id,
            "capability": self.capability,
            "principal": self.principal,
            "state": self.state.value,
            "terminal": self.terminal,
            "created_at": iso(self.created_at),
            "started_at": iso(self.started_at),
            "updated_at": iso(self.updated_at),
            "finished_at": iso(self.finished_at),
            "result_ref": self.result_ref,
            "cause": self.cause,
            "budget": self.budget,
        }


@dataclass(frozen=True)
class RunEvent:
    run_id: str
    kind: str
    at: datetime
    data: dict[str, Any] = field(default_factory=dict)
