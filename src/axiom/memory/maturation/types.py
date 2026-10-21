# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Maturation-lifecycle public types — per `spec-memory-maturation.md` §3.

Stage enumeration, canonical ordering, the StageHandler protocol that mat-2,
mat-3, mat-4 implementations satisfy, and the report / budget dataclasses
the orchestrator emits.

Kept dependency-light so handler implementations can import from here
without pulling in the full orchestrator.
"""

from __future__ import annotations

from enum import Enum
from typing import NamedTuple, Protocol, runtime_checkable


class Stage(Enum):
    """The 7 canonical maturation stages (per `spec-memory-maturation.md §3`).

    Stages 1 (Capture) and 2 (Score, when synchronous) happen on the write hot
    path and are not part of the dream cycle. Stages enumerated here all run
    asynchronously inside :class:`DreamCycleOrchestrator`.
    """

    IMPORTANCE_SCORING = "importance_scoring"          # stage 2 (async catch-up)
    CONSOLIDATION_DAILY = "consolidation_daily"        # stage 3 daily cadence
    CONSOLIDATION_WEEKLY = "consolidation_weekly"      # stage 3 weekly cadence
    CONSOLIDATION_MONTHLY = "consolidation_monthly"    # stage 3 monthly (identity)
    COMPACTION_SUMMARIZE = "compaction_summarize"      # stage 4
    COMPACTION_ARCHIVE = "compaction_archive"          # stage 5
    COMPACTION_FORGET = "compaction_forget"            # stage 6


#: Canonical execution order. The orchestrator runs registered stages in this
#: order regardless of registration order. Earlier stages must complete before
#: later stages (e.g., compaction won't tombstone an episode while
#: consolidation hasn't yet derived its semantic).
STAGE_ORDER: tuple[Stage, ...] = (
    Stage.IMPORTANCE_SCORING,
    Stage.CONSOLIDATION_DAILY,
    Stage.CONSOLIDATION_WEEKLY,
    Stage.CONSOLIDATION_MONTHLY,
    Stage.COMPACTION_SUMMARIZE,
    Stage.COMPACTION_ARCHIVE,
    Stage.COMPACTION_FORGET,
)


class CycleBudget(NamedTuple):
    """Per-cycle resource caps. Handlers must stop cleanly when exceeded.

    ``max_calls`` — LLM call count cap across the cycle.
    ``max_tokens`` — LLM token cap across the cycle.
    ``max_walltime_seconds`` — wall-clock cap across the cycle.

    A ``CycleBudget(0, 0, 0)`` means "no budget" — only deterministic
    handlers can run, and walltime is unlimited. The default budget passed
    by the orchestrator when no explicit budget is supplied is generous.
    """

    max_calls: int = 1000
    max_tokens: int = 1_000_000
    max_walltime_seconds: float = 600.0


class BudgetExceededError(Exception):
    """Raised by a stage handler when its work exceeds the remaining budget.

    The orchestrator catches this, records the interruption in the cycle
    report, and stops cleanly (no further stages run).
    """


class StageResult(NamedTuple):
    """Per-stage report from one cycle's invocation of a handler."""

    stage: Stage
    scope: str
    started_at: str           # ISO 8601
    completed_at: str         # ISO 8601
    items_processed: int
    items_succeeded: int
    items_failed: int
    calls_used: int = 0
    tokens_used: int = 0
    notes: str = ""


class CycleReport(NamedTuple):
    """Orchestrator's report for one full cycle in one scope."""

    scope: str
    started_at: str
    completed_at: str
    stages_run: tuple[StageResult, ...]
    budget_consumed_calls: int = 0
    budget_consumed_tokens: int = 0
    budget_consumed_walltime_s: float = 0.0
    interrupted: bool = False
    interruption_reason: str | None = None


@runtime_checkable
class StageHandler(Protocol):
    """Protocol for stage implementations.

    Each handler owns one :class:`Stage`. The orchestrator coordinates;
    handlers do the actual work. See ``spec-memory-maturation.md §6``.
    """

    stage: Stage

    def is_pending(self, scope: str) -> bool:
        """Does this stage have pending work for ``scope``?

        Called once per cycle per scope before :meth:`evaluate_trigger`.
        Cheap; should not consume cycle budget.
        """
        ...

    def evaluate_trigger(self, scope: str) -> bool:
        """Should this stage fire for ``scope`` right now?

        Called only when :meth:`is_pending` returned ``True``. The handler
        owns its own trigger semantics (time-based, count-based,
        importance-threshold, hybrid). Cheap; should not consume cycle
        budget.
        """
        ...

    def run(self, scope: str, budget: CycleBudget) -> StageResult:
        """Run the stage for ``scope`` within the remaining ``budget``.

        Raises :class:`BudgetExceededError` if the work cannot complete
        within ``budget`` — the orchestrator records the interruption and
        stops the cycle cleanly.
        """
        ...
