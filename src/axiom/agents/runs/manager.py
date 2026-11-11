# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``RunManager``: submit, draw, advance and cancel runs against a budget.

Ties the three A0 pieces together: the envelope on a ``CapabilityToken``
(B1), the ledger that meters it (B2, B3, B4), and the :class:`RunHandle` that
reports it (H1). Nothing here raises across a surface for a budget reason:
a refusal is returned as ``unmet`` in the ADR-185 shape.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from axiom.agents.runs.handle import RunEvent, RunHandle, RunState, new_run_id
from axiom.agents.runs.store import InMemoryRunStore, RunStore
from axiom.governance.budget import (
    BUDGET_EXHAUSTED,
    BudgetEnvelope,
    BudgetLedger,
    DrawOutcome,
    required_unmet,
)
from axiom.governance.capability import CapabilityToken

RUN_NOT_FOUND = "run_not_found"
RUN_FINISHED = "run_finished"


def _not_found() -> dict[str, str]:
    return {"id": RUN_NOT_FOUND, "why": "no such run", "fix": "check the run id"}


def _finished(handle: RunHandle) -> dict[str, str]:
    return {
        "id": RUN_FINISHED,
        "why": f"run is already {handle.state.value}",
        "fix": "start a new run",
    }


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Submission:
    """The outcome of :meth:`RunManager.submit`: a handle, or the reasons it was refused."""

    handle: RunHandle | None = None
    unmet: tuple[dict[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return self.handle is not None


class RunManager:
    def __init__(
        self,
        store: RunStore | None = None,
        *,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.store: RunStore = store if store is not None else InMemoryRunStore()
        self._now = now
        self._lock = threading.RLock()
        # Ledgers are live accounting and stay in this process. Only their
        # snapshots are persisted on the handle (see RunStore docstring).
        self._ledgers: dict[str, BudgetLedger] = {}

    # ---- submit ----------------------------------------------------------

    def submit(
        self,
        capability: str,
        *,
        token: CapabilityToken,
        principal: str,
        parent_id: str | None = None,
        requested_budget: BudgetEnvelope | None = None,
        agent_backed: bool = True,
    ) -> Submission:
        """Create a run in ``submitted``.

        An agent-backed run REFUSES to start without an envelope
        (``budget_required``). With a ``parent_id`` the run's grant is a slice of
        the parent's remaining amounts, reserved now and refunded at completion.
        A non-agent run needs none and is unbounded.
        """
        with self._lock:
            ledger: BudgetLedger | None = None
            if agent_backed:
                parent = self.store.get(parent_id) if parent_id is not None else None
                if parent_id is not None and parent is None:
                    return Submission(unmet=(_not_found(),))
                if parent is not None and parent.terminal:
                    return Submission(unmet=(_finished(parent),))
                parent_ledger = self._ledgers.get(parent_id) if parent_id else None
                if parent_ledger is not None:
                    gate = parent_ledger.check()
                    if not gate.ok:
                        return Submission(unmet=gate.unmet)
                    ledger = parent_ledger.slice(requested_budget)
                else:
                    if token.budget is None:
                        return Submission(unmet=(required_unmet(),))
                    # A root run never gets more than its token grants.
                    grant = (
                        token.budget
                        if requested_budget is None
                        else token.budget.intersect(requested_budget)
                    )
                    ledger = BudgetLedger(grant)
            now = self._now()
            handle = RunHandle(
                id=new_run_id(),
                capability=capability,
                principal=principal,
                state=RunState.SUBMITTED,
                created_at=now,
                updated_at=now,
                parent_id=parent_id,
                budget=ledger.snapshot() if ledger else None,
            )
            if ledger is not None:
                self._ledgers[handle.id] = ledger
            self.store.put(handle)
            self._event(handle.id, "submitted")
            return Submission(handle=handle)

    # ---- reads -----------------------------------------------------------

    def get(self, run_id: str) -> RunHandle | None:
        with self._lock:
            handle = self.store.get(run_id)
            return self._refreshed(handle) if handle else None

    def list(
        self, *, state: RunState | None = None, parent_id: str | None = None
    ) -> list[RunHandle]:
        with self._lock:
            return [self._refreshed(h) for h in self.store.list(state=state, parent_id=parent_id)]

    def events(self, run_id: str) -> list[RunEvent]:
        return self.store.events(run_id)

    def remaining(self, run_id: str) -> BudgetEnvelope | None:
        ledger = self._ledgers.get(run_id)
        return ledger.remaining() if ledger else None

    # ---- budget ----------------------------------------------------------

    def draw(
        self,
        run_id: str,
        *,
        turns: float = 0,
        tokens: float = 0,
        dollars: float = 0,
        wall_clock_seconds: float = 0,
    ) -> DrawOutcome:
        """Draw at a step boundary. A refusal fails the run with cause ``budget_exhausted``.

        The failing happens at the boundary: the step that was allowed to begin
        has already finished, so no half-finished action is cut off.
        """
        with self._lock:
            handle = self.store.get(run_id)
            if handle is None:
                return DrawOutcome(ok=False, unmet=(_not_found(),))
            ledger = self._ledgers.get(run_id)
            if ledger is None:
                return DrawOutcome(ok=True)  # unbudgeted run
            if handle.terminal:
                return DrawOutcome(ok=False, unmet=(_finished(handle),))
            ledger.tick()
            outcome = ledger.draw(
                turns=turns, tokens=tokens, dollars=dollars, wall_clock_seconds=wall_clock_seconds
            )
            if not outcome.ok:
                self._finish(handle, RunState.FAILED, cause=BUDGET_EXHAUSTED)
                self._event(run_id, BUDGET_EXHAUSTED, {"limit": outcome.limit})
            else:
                self.store.put(replace(handle, budget=ledger.snapshot(), updated_at=self._now()))
            return outcome

    # ---- state -----------------------------------------------------------

    def start(self, run_id: str) -> RunHandle | None:
        return self.advance(run_id, RunState.WORKING)

    def advance(
        self,
        run_id: str,
        state: RunState,
        *,
        cause: str | None = None,
        result_ref: str | None = None,
    ) -> RunHandle | None:
        with self._lock:
            handle = self.store.get(run_id)
            if handle is None:
                return None
            if state.terminal:
                return self._finish(handle, state, cause=cause, result_ref=result_ref)
            updated = handle.advance(state, now=self._now(), cause=cause)
            self.store.put(updated)
            self._event(run_id, state.value)
            return updated

    def cancel(self, run_id: str, *, cause: str = "cancelled_by_caller") -> Submission:
        """Cancel a run that is not finished. A finished run is reported, not changed."""
        with self._lock:
            handle = self.store.get(run_id)
            if handle is None:
                return Submission(unmet=(_not_found(),))
            if handle.terminal:
                return Submission(unmet=(_finished(handle),))
            return Submission(handle=self._finish(handle, RunState.CANCELLED, cause=cause))

    # ---- internals -------------------------------------------------------

    def _finish(
        self,
        handle: RunHandle,
        state: RunState,
        *,
        cause: str | None = None,
        result_ref: str | None = None,
    ) -> RunHandle:
        """Move to a terminal state and refund the unused budget to the parent, once."""
        updated = handle.advance(state, now=self._now(), cause=cause, result_ref=result_ref)
        ledger = self._ledgers.get(handle.id)
        if ledger is not None:
            ledger.close()
            updated = replace(updated, budget=ledger.snapshot())
        self.store.put(updated)
        self._event(handle.id, state.value, {"cause": cause} if cause else {})
        return updated

    def _refreshed(self, handle: RunHandle) -> RunHandle:
        ledger = self._ledgers.get(handle.id)
        if ledger is None or handle.terminal:
            return handle
        return replace(handle, budget=ledger.snapshot())

    def _event(self, run_id: str, kind: str, data: dict | None = None) -> None:
        self.store.append_event(RunEvent(run_id=run_id, kind=kind, at=self._now(), data=data or {}))


__all__ = ["RUN_FINISHED", "RUN_NOT_FOUND", "RunManager", "Submission"]
