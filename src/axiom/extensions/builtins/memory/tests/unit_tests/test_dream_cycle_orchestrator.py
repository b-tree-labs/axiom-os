# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the dream-cycle orchestrator (mat-1).

Per spec-memory-maturation.md §6, the dream cycle is the unified low-activity
pass that runs maturation stages in order for a scope, gated by per-stage
triggers and per-cycle budgets. The orchestrator itself doesn't run stages —
it coordinates them. Stage handlers (importance scoring, reflection,
compaction, …) plug in via the StageHandler protocol.

This test file owns mat-1's contract:

- Stage ordering (canonical, deterministic)
- Trigger evaluation (per-stage, scope-aware)
- Stage handler registration + dispatch
- Budget enforcement (calls + tokens + walltime; stops cleanly at stage
  boundary when exceeded)
- Cycle metrics fragment written to the ledger after each cycle
- Cooldown semantics
- Cycle state preservation across host restart
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_composition(tmp_path: Path):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    base = tmp_path / "memory"
    base.mkdir()
    kp = generate_keypair()
    reg = ArtifactRegistry(backend=SQLiteBackend(base / "artifacts.db"))
    audit = AuditLog(base / "audit.jsonl", signing_keypair=kp)
    return CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


# ---------------------------------------------------------------------------
# Module-level surface
# ---------------------------------------------------------------------------


def test_maturation_module_imports():
    """The maturation submodule and its top-level exports are importable."""
    from axiom.memory.maturation import (
        STAGE_ORDER,
        BudgetExceededError,
        CycleReport,
        DreamCycleOrchestrator,
        Stage,
        StageHandler,
        StageResult,
    )

    assert Stage is not None
    assert StageHandler is not None
    assert StageResult is not None
    assert CycleReport is not None
    assert BudgetExceededError is not None
    assert DreamCycleOrchestrator is not None
    assert STAGE_ORDER is not None


def test_stage_enum_has_canonical_members():
    """The 7 maturation stages from spec-memory-maturation.md §3 are present."""
    from axiom.memory.maturation import Stage

    expected = {
        "IMPORTANCE_SCORING",
        "CONSOLIDATION_DAILY",
        "CONSOLIDATION_WEEKLY",
        "CONSOLIDATION_MONTHLY",
        "COMPACTION_SUMMARIZE",
        "COMPACTION_ARCHIVE",
        "COMPACTION_FORGET",
    }
    assert {m.name for m in Stage} == expected


def test_stage_order_is_canonical():
    """Stages run in dependency order: score → consolidate → compact → archive → forget."""
    from axiom.memory.maturation import STAGE_ORDER, Stage

    assert STAGE_ORDER == (
        Stage.IMPORTANCE_SCORING,
        Stage.CONSOLIDATION_DAILY,
        Stage.CONSOLIDATION_WEEKLY,
        Stage.CONSOLIDATION_MONTHLY,
        Stage.COMPACTION_SUMMARIZE,
        Stage.COMPACTION_ARCHIVE,
        Stage.COMPACTION_FORGET,
    )


# ---------------------------------------------------------------------------
# Orchestrator construction
# ---------------------------------------------------------------------------


def test_orchestrator_constructs_with_composition(isolated_composition):
    from axiom.memory.maturation import DreamCycleOrchestrator

    orch = DreamCycleOrchestrator(composition=isolated_composition)
    assert orch.composition is isolated_composition
    # No stage handlers registered by default — the orchestrator is a
    # coordination surface; handlers come from mat-2/3/4.
    assert orch.registered_stages() == ()


def test_orchestrator_registers_stage_handler(isolated_composition):
    from axiom.memory.maturation import DreamCycleOrchestrator, Stage

    orch = DreamCycleOrchestrator(composition=isolated_composition)
    handler = _StubHandler(Stage.IMPORTANCE_SCORING)
    orch.register(handler)

    assert orch.registered_stages() == (Stage.IMPORTANCE_SCORING,)
    assert orch.handler_for(Stage.IMPORTANCE_SCORING) is handler


def test_orchestrator_rejects_duplicate_registration(isolated_composition):
    from axiom.memory.maturation import DreamCycleOrchestrator, Stage

    orch = DreamCycleOrchestrator(composition=isolated_composition)
    orch.register(_StubHandler(Stage.IMPORTANCE_SCORING))

    with pytest.raises(ValueError, match="already registered"):
        orch.register(_StubHandler(Stage.IMPORTANCE_SCORING))


# ---------------------------------------------------------------------------
# Cycle execution — empty + simple
# ---------------------------------------------------------------------------


def test_run_cycle_with_no_handlers_returns_empty(isolated_composition):
    """No handlers registered → cycle runs but no stages execute."""
    from axiom.memory.maturation import DreamCycleOrchestrator

    orch = DreamCycleOrchestrator(composition=isolated_composition)
    report = orch.run_cycle(scope="test-scope")

    assert report.scope == "test-scope"
    assert report.stages_run == ()
    assert report.interrupted is False


def test_run_cycle_skips_stages_with_no_pending_work(isolated_composition):
    """A handler whose is_pending() returns False is skipped."""
    from axiom.memory.maturation import DreamCycleOrchestrator, Stage

    orch = DreamCycleOrchestrator(composition=isolated_composition)
    handler = _StubHandler(Stage.IMPORTANCE_SCORING, is_pending=False)
    orch.register(handler)

    report = orch.run_cycle(scope="test-scope")
    assert report.stages_run == ()
    assert handler.run_calls == 0


def test_run_cycle_runs_pending_stage(isolated_composition):
    from axiom.memory.maturation import DreamCycleOrchestrator, Stage

    orch = DreamCycleOrchestrator(composition=isolated_composition)
    handler = _StubHandler(
        Stage.IMPORTANCE_SCORING, is_pending=True, trigger_fires=True
    )
    orch.register(handler)

    report = orch.run_cycle(scope="test-scope")
    assert len(report.stages_run) == 1
    assert report.stages_run[0].stage == Stage.IMPORTANCE_SCORING
    assert handler.run_calls == 1


def test_run_cycle_skips_stage_when_trigger_does_not_fire(isolated_composition):
    """A handler that is_pending but trigger doesn't fire is skipped."""
    from axiom.memory.maturation import DreamCycleOrchestrator, Stage

    orch = DreamCycleOrchestrator(composition=isolated_composition)
    handler = _StubHandler(
        Stage.IMPORTANCE_SCORING, is_pending=True, trigger_fires=False
    )
    orch.register(handler)

    report = orch.run_cycle(scope="test-scope")
    assert report.stages_run == ()
    assert handler.run_calls == 0


# ---------------------------------------------------------------------------
# Stage ordering
# ---------------------------------------------------------------------------


def test_run_cycle_runs_stages_in_canonical_order(isolated_composition):
    """Registration order doesn't matter — canonical STAGE_ORDER governs execution."""
    from axiom.memory.maturation import DreamCycleOrchestrator, Stage

    orch = DreamCycleOrchestrator(composition=isolated_composition)
    # Register in reverse order
    h_forget = _StubHandler(Stage.COMPACTION_FORGET, is_pending=True, trigger_fires=True)
    h_summarize = _StubHandler(Stage.COMPACTION_SUMMARIZE, is_pending=True, trigger_fires=True)
    h_score = _StubHandler(Stage.IMPORTANCE_SCORING, is_pending=True, trigger_fires=True)
    orch.register(h_forget)
    orch.register(h_summarize)
    orch.register(h_score)

    report = orch.run_cycle(scope="test-scope")
    stages_run = [r.stage for r in report.stages_run]
    assert stages_run == [
        Stage.IMPORTANCE_SCORING,
        Stage.COMPACTION_SUMMARIZE,
        Stage.COMPACTION_FORGET,
    ]


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


def test_run_cycle_respects_call_budget(isolated_composition):
    """Cycle stops cleanly at stage boundary when call budget is exhausted."""
    from axiom.memory.maturation import (
        CycleBudget,
        DreamCycleOrchestrator,
        Stage,
    )

    orch = DreamCycleOrchestrator(composition=isolated_composition)
    # Two stages registered. Stage A burns the whole budget; B doesn't run.
    h_a = _StubHandler(
        Stage.IMPORTANCE_SCORING,
        is_pending=True,
        trigger_fires=True,
        budget_calls_used=5,
    )
    h_b = _StubHandler(
        Stage.CONSOLIDATION_DAILY, is_pending=True, trigger_fires=True
    )
    orch.register(h_a)
    orch.register(h_b)

    budget = CycleBudget(max_calls=5, max_tokens=10000, max_walltime_seconds=60)
    report = orch.run_cycle(scope="test-scope", budget=budget)

    # First stage ran and consumed the budget; second was skipped.
    assert len(report.stages_run) == 1
    assert report.stages_run[0].stage == Stage.IMPORTANCE_SCORING
    assert h_b.run_calls == 0
    assert report.budget_consumed_calls == 5


def test_run_cycle_marks_interrupted_when_handler_raises_budget(isolated_composition):
    from axiom.memory.maturation import (
        CycleBudget,
        DreamCycleOrchestrator,
        Stage,
    )

    orch = DreamCycleOrchestrator(composition=isolated_composition)
    handler = _BudgetExceedingHandler(Stage.IMPORTANCE_SCORING)
    orch.register(handler)

    budget = CycleBudget(max_calls=100, max_tokens=10000, max_walltime_seconds=60)
    report = orch.run_cycle(scope="test-scope", budget=budget)
    assert report.interrupted is True
    assert "budget" in (report.interruption_reason or "").lower()


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------


def test_run_cycle_respects_cooldown(isolated_composition):
    """A second cycle within the cooldown window returns immediately."""
    from axiom.memory.maturation import DreamCycleOrchestrator, Stage

    orch = DreamCycleOrchestrator(
        composition=isolated_composition, cooldown_seconds=3600
    )
    handler = _StubHandler(
        Stage.IMPORTANCE_SCORING, is_pending=True, trigger_fires=True
    )
    orch.register(handler)

    report_1 = orch.run_cycle(scope="test-scope")
    assert len(report_1.stages_run) == 1

    report_2 = orch.run_cycle(scope="test-scope")
    # No stages ran the second time — cooldown blocks
    assert report_2.stages_run == ()
    assert handler.run_calls == 1


def test_run_cycle_force_bypasses_cooldown(isolated_composition):
    from axiom.memory.maturation import DreamCycleOrchestrator, Stage

    orch = DreamCycleOrchestrator(
        composition=isolated_composition, cooldown_seconds=3600
    )
    handler = _StubHandler(
        Stage.IMPORTANCE_SCORING, is_pending=True, trigger_fires=True
    )
    orch.register(handler)

    orch.run_cycle(scope="test-scope")
    report_2 = orch.run_cycle(scope="test-scope", force=True)
    assert len(report_2.stages_run) == 1
    assert handler.run_calls == 2


# ---------------------------------------------------------------------------
# Cycle metrics fragment
# ---------------------------------------------------------------------------


def test_run_cycle_writes_metrics_fragment(isolated_composition):
    """After a cycle, a fact_kind='dream_cycle_metrics' fragment is in the ledger."""
    from axiom.memory.maturation import DreamCycleOrchestrator, Stage

    orch = DreamCycleOrchestrator(composition=isolated_composition)
    handler = _StubHandler(
        Stage.IMPORTANCE_SCORING, is_pending=True, trigger_fires=True
    )
    orch.register(handler)

    orch.run_cycle(scope="test-scope", principal_id="ben@example.com")

    fragments = list(isolated_composition.artifact_registry.list(kind="fragment"))
    metric_fragments = [
        a for a in fragments
        if (a.data or {}).get("content", {}).get("fact_kind") == "dream_cycle_metrics"
    ]
    assert len(metric_fragments) == 1
    content = metric_fragments[0].data["content"]
    assert content["scope"] == "test-scope"
    assert "stages_run" in content


# ---------------------------------------------------------------------------
# Helpers — stub handlers for orchestrator-level testing
# ---------------------------------------------------------------------------


class _StubHandler:
    """Minimal StageHandler implementation for orchestrator-only tests."""

    def __init__(
        self,
        stage,
        *,
        is_pending: bool = True,
        trigger_fires: bool = True,
        budget_calls_used: int = 0,
        budget_tokens_used: int = 0,
    ):
        self.stage = stage
        self._is_pending = is_pending
        self._trigger_fires = trigger_fires
        self._budget_calls_used = budget_calls_used
        self._budget_tokens_used = budget_tokens_used
        self.run_calls = 0

    def is_pending(self, scope: str) -> bool:
        return self._is_pending

    def evaluate_trigger(self, scope: str) -> bool:
        return self._trigger_fires

    def run(self, scope: str, budget):
        from datetime import datetime, timezone

        from axiom.memory.maturation import StageResult

        self.run_calls += 1
        now = datetime.now(timezone.utc).isoformat()
        return StageResult(
            stage=self.stage,
            scope=scope,
            started_at=now,
            completed_at=now,
            items_processed=1,
            items_succeeded=1,
            items_failed=0,
            calls_used=self._budget_calls_used,
            tokens_used=self._budget_tokens_used,
            notes="stub",
        )


class _BudgetExceedingHandler:
    """Handler that raises BudgetExceededError mid-run."""

    def __init__(self, stage):
        self.stage = stage

    def is_pending(self, scope: str) -> bool:
        return True

    def evaluate_trigger(self, scope: str) -> bool:
        return True

    def run(self, scope: str, budget):
        from axiom.memory.maturation import BudgetExceededError

        raise BudgetExceededError("simulated budget exhaustion")
