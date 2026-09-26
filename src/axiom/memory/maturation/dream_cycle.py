# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Dream-cycle orchestrator.

Per `spec-memory-maturation.md §6`, the dream cycle is the unified
low-activity pass that runs maturation stage handlers in canonical
:data:`STAGE_ORDER` for a scope, gated by per-stage triggers and per-cycle
budgets. The orchestrator itself never runs stage logic — it dispatches
to registered :class:`StageHandler` implementations.

The orchestrator:

- Accepts registrations of one :class:`StageHandler` per :class:`Stage`
- Runs a cycle for a given scope, walking stages in canonical order
- Skips a stage when its handler reports ``not is_pending`` or
  ``not evaluate_trigger``
- Tracks cumulative budget consumption across stages; stops cleanly at
  a stage boundary if exhausted
- Catches :class:`BudgetExceededError` from a mid-stage handler and
  marks the cycle ``interrupted``
- Honors a per-scope cooldown to prevent thrash from a successful cycle
  immediately re-firing
- Writes a ``fact_kind="dream_cycle_metrics"`` fragment to the ledger
  after each cycle (audit + SCAN monitoring surface)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from .types import (
    STAGE_ORDER,
    BudgetExceededError,
    CycleBudget,
    CycleReport,
    Stage,
    StageHandler,
    StageResult,
)

_DEFAULT_COOLDOWN_SECONDS = 60.0


class DreamCycleOrchestrator:
    """Coordinates maturation stage handlers per scope.

    Construct once per ``CompositionService`` (typically one per ``axi``
    process). Register stage handlers via :meth:`register`. Drive cycles
    via :meth:`run_cycle`.

    Args:
        composition: the :class:`axiom.memory.composition.CompositionService`
            instance whose ledger this orchestrator operates against.
        cooldown_seconds: minimum seconds between successful cycles for a
            given scope. Defaults to 60s; ``force=True`` on
            :meth:`run_cycle` bypasses it.
    """

    def __init__(
        self,
        composition: Any,  # CompositionService — typed Any to avoid circular import
        *,
        cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        self.composition = composition
        self.cooldown_seconds = cooldown_seconds
        self._handlers: dict[Stage, StageHandler] = {}
        # Per-scope monotonic timestamp of last successful cycle (epoch seconds).
        self._last_cycle_at: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, handler: StageHandler) -> None:
        """Register a :class:`StageHandler` to its declared :class:`Stage`.

        Raises:
            ValueError: if a handler for the same stage is already registered.
        """
        stage = handler.stage
        if stage in self._handlers:
            raise ValueError(
                f"a handler for stage {stage.name} is already registered "
                f"({type(self._handlers[stage]).__name__})"
            )
        self._handlers[stage] = handler

    def registered_stages(self) -> tuple[Stage, ...]:
        """Return the stages with a handler registered, in canonical order."""
        return tuple(s for s in STAGE_ORDER if s in self._handlers)

    def handler_for(self, stage: Stage) -> StageHandler | None:
        """Return the handler registered for ``stage``, or ``None``."""
        return self._handlers.get(stage)

    # ------------------------------------------------------------------
    # Cycle execution
    # ------------------------------------------------------------------

    def run_cycle(
        self,
        scope: str,
        *,
        budget: CycleBudget | None = None,
        force: bool = False,
        principal_id: str = "axiom-system",
    ) -> CycleReport:
        """Run one dream cycle for ``scope``.

        Stages execute in canonical order. Each registered stage's
        :meth:`StageHandler.is_pending` and :meth:`StageHandler.evaluate_trigger`
        are consulted before :meth:`StageHandler.run` is called.

        Cooldown is enforced unless ``force=True``.

        Always writes a cycle-metrics fragment to the ledger.

        Args:
            scope: the scope to run the cycle for.
            budget: per-cycle resource cap. Defaults to a generous
                :class:`CycleBudget`.
            force: bypass cooldown.
            principal_id: principal for the cycle-metrics fragment write.

        Returns:
            :class:`CycleReport` describing what ran.
        """
        budget = budget or CycleBudget()
        cycle_start_wall = time.monotonic()
        cycle_start_iso = datetime.now(timezone.utc).isoformat()

        if not force and self._is_in_cooldown(scope, now=cycle_start_wall):
            report = CycleReport(
                scope=scope,
                started_at=cycle_start_iso,
                completed_at=cycle_start_iso,
                stages_run=(),
                interrupted=False,
                interruption_reason=None,
            )
            self._write_metrics_fragment(report, principal_id, reason="cooldown")
            return report

        stages_run: list[StageResult] = []
        calls_consumed = 0
        tokens_consumed = 0
        interrupted = False
        interruption_reason: str | None = None

        for stage in STAGE_ORDER:
            handler = self._handlers.get(stage)
            if handler is None:
                continue
            if not handler.is_pending(scope):
                continue
            if not handler.evaluate_trigger(scope):
                continue

            # Check budget before invoking the handler — if we're already
            # over, stop cleanly without invoking another handler.
            elapsed = time.monotonic() - cycle_start_wall
            remaining_calls = budget.max_calls - calls_consumed
            remaining_tokens = budget.max_tokens - tokens_consumed
            remaining_walltime = budget.max_walltime_seconds - elapsed
            if (
                remaining_calls <= 0
                or remaining_tokens <= 0
                or remaining_walltime <= 0.0
            ):
                interrupted = True
                interruption_reason = "budget exhausted at stage boundary"
                break

            remaining_budget = CycleBudget(
                max_calls=remaining_calls,
                max_tokens=remaining_tokens,
                max_walltime_seconds=remaining_walltime,
            )

            try:
                result = handler.run(scope, remaining_budget)
            except BudgetExceededError as exc:
                interrupted = True
                interruption_reason = f"budget exceeded mid-stage: {exc}"
                break

            stages_run.append(result)
            calls_consumed += result.calls_used
            tokens_consumed += result.tokens_used

        cycle_end_iso = datetime.now(timezone.utc).isoformat()
        wall_elapsed = time.monotonic() - cycle_start_wall

        if not interrupted:
            self._last_cycle_at[scope] = time.monotonic()

        report = CycleReport(
            scope=scope,
            started_at=cycle_start_iso,
            completed_at=cycle_end_iso,
            stages_run=tuple(stages_run),
            budget_consumed_calls=calls_consumed,
            budget_consumed_tokens=tokens_consumed,
            budget_consumed_walltime_s=wall_elapsed,
            interrupted=interrupted,
            interruption_reason=interruption_reason,
        )
        self._write_metrics_fragment(report, principal_id)
        return report

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _is_in_cooldown(self, scope: str, *, now: float) -> bool:
        last = self._last_cycle_at.get(scope)
        if last is None:
            return False
        return (now - last) < self.cooldown_seconds

    def _write_metrics_fragment(
        self,
        report: CycleReport,
        principal_id: str,
        reason: str | None = None,
    ) -> None:
        """Persist a cycle-metrics fragment so SCAN + TRIAGE can observe."""
        content: dict[str, Any] = {
            "fact_kind": "dream_cycle_metrics",
            "event_time": report.completed_at,
            "scope": report.scope,
            "started_at": report.started_at,
            "completed_at": report.completed_at,
            "stages_run": [
                {
                    "stage": r.stage.value,
                    "items_processed": r.items_processed,
                    "items_succeeded": r.items_succeeded,
                    "items_failed": r.items_failed,
                    "calls_used": r.calls_used,
                    "tokens_used": r.tokens_used,
                    "notes": r.notes,
                }
                for r in report.stages_run
            ],
            "budget_consumed_calls": report.budget_consumed_calls,
            "budget_consumed_tokens": report.budget_consumed_tokens,
            "budget_consumed_walltime_s": report.budget_consumed_walltime_s,
            "interrupted": report.interrupted,
            "interruption_reason": report.interruption_reason,
        }
        if reason is not None:
            content["skipped_reason"] = reason

        self.composition.write(
            content=content,
            cognitive_type="episodic",
            principal_id=principal_id,
            agents={"axi-memory-dream-cycle"},
            resources=set(),
        )
