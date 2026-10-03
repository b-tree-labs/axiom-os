# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Memory maturation lifecycle.

Implements the dream cycle (`spec-memory-maturation.md §6`) — the unified
low-activity orchestrator that runs stage handlers in canonical order for a
scope, gated by per-stage triggers and per-cycle budgets.

Submodules:

- :mod:`types` — Stage enum, StageHandler protocol, CycleReport, CycleBudget
- :mod:`dream_cycle` — :class:`DreamCycleOrchestrator`

Public surface (mat-1):

- ``Stage`` — the 7 canonical maturation stages
- ``STAGE_ORDER`` — canonical execution order
- ``StageHandler`` — the protocol stage implementations satisfy
- ``StageResult`` — per-stage report (one per stage that ran)
- ``CycleReport`` — orchestrator's overall report for a cycle
- ``CycleBudget`` — per-cycle cost cap
- ``BudgetExceededError`` — raised by handlers when budget runs out mid-run
- ``DreamCycleOrchestrator`` — the orchestrator class

The actual stage handlers (importance scoring, reflection, compaction)
register with the orchestrator via :meth:`DreamCycleOrchestrator.register`
and are implemented in their own submodules (mat-2, mat-3, mat-4).
"""

from .types import (
    STAGE_ORDER,
    BudgetExceededError,
    CycleBudget,
    CycleReport,
    Stage,
    StageHandler,
    StageResult,
)
from .dream_cycle import DreamCycleOrchestrator

__all__ = [
    "STAGE_ORDER",
    "BudgetExceededError",
    "CycleBudget",
    "CycleReport",
    "Stage",
    "StageHandler",
    "StageResult",
    "DreamCycleOrchestrator",
]
