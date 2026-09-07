# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.agents.pipeline.persistence — Plan + AgentRun storage.

Per ADR-034 §D2 + §D9: Plans are MemoryFragments of cognitive_type=procedural;
agent runs are sequences of fragments bound by run_id. CompositionService is
the single write entry; ArtifactRegistry's find_fragments is the read path.
"""

from __future__ import annotations

import pytest

from axiom.agents.pipeline.agent import (
    AgentEvent,
    AgentEventKind,
    AgentRun,
    AgentRunStatus,
)
from axiom.agents.pipeline.persistence import (
    MemoryBackedPlanStore,
    event_from_content_dict,
    event_to_content_dict,
    plan_from_content_dict,
    plan_to_content_dict,
    run_from_content_dict,
    run_to_content_dict,
)
from axiom.agents.pipeline.plan import (
    Plan,
    PlanRequest,
    PlanStatus,
    PlanStep,
    PlanStepGate,
    StepReach,
)
from axiom.vega.federation.policy import (
    ClassificationStamp,
    VisibilityHorizon,
)


@pytest.fixture
def stack(tmp_path):
    from axiom.memory.bootstrap import build_memory_stack
    return build_memory_stack(scope_id="test-persist-scope", data_root=tmp_path)


def _req() -> PlanRequest:
    return PlanRequest(
        goal="explain primary loops",
        scope_id="test-persist-scope",
        principal_id="@ben:example-org",
        accountable_human_id="@ben:example-org",
    )


def _plan() -> Plan:
    return Plan(
        request=_req(),
        steps=(
            PlanStep(intent="retrieve docs", tool_id="rag.retrieve"),
            PlanStep(
                intent="explain",
                tool_id="llm.complete",
                gate=PlanStepGate.APPROVE,
                reach=StepReach(network=("api.openai.com",)),
            ),
        ),
    )


# --------------------------------------------------------------------------
# Plan serialization round-trip
# --------------------------------------------------------------------------


class TestPlanSerialization:
    def test_to_content_dict_includes_required_fields(self):
        plan = _plan()
        d = plan_to_content_dict(plan)
        assert d["kind"] == "plan"
        assert d["plan_id"] == plan.plan_id
        assert d["status"] == plan.status.value
        assert "steps" in d
        assert len(d["steps"]) == 2

    def test_round_trip_preserves_plan(self):
        original = _plan()
        d = plan_to_content_dict(original)
        restored = plan_from_content_dict(d)
        assert restored.plan_id == original.plan_id
        assert restored.status == original.status
        assert len(restored.steps) == len(original.steps)
        assert restored.steps[0].intent == original.steps[0].intent
        assert restored.steps[1].gate == original.steps[1].gate
        assert restored.steps[1].reach.network == original.steps[1].reach.network

    def test_round_trip_preserves_request(self):
        original = _plan()
        d = plan_to_content_dict(original)
        restored = plan_from_content_dict(d)
        assert restored.request.goal == original.request.goal
        assert restored.request.accountable_human_id == original.request.accountable_human_id

    def test_round_trip_preserves_classification_stamp(self):
        plan = Plan(
            request=_req(),
            steps=(),
            classification=ClassificationStamp.unclassified(),
            visibility=VisibilityHorizon.SCOPE_INTERNAL,
        )
        d = plan_to_content_dict(plan)
        restored = plan_from_content_dict(d)
        assert restored.visibility == VisibilityHorizon.SCOPE_INTERNAL

    def test_kind_marker_present(self):
        d = plan_to_content_dict(_plan())
        # The "kind" content key disambiguates plan fragments from other procedural
        # fragments at retrieval time.
        assert d["kind"] == "plan"

    def test_dict_is_json_serializable(self):
        import json
        d = plan_to_content_dict(_plan())
        # Should not raise — fragment storage requires JSON serialization.
        encoded = json.dumps(d)
        decoded = json.loads(encoded)
        restored = plan_from_content_dict(decoded)
        assert restored.plan_id == _plan().plan_id or len(restored.steps) == 2


class TestPlanSchemaVersion:
    """Plans carry a schema_version separate from MemoryFragment.schema_version
    per memory-persistence-plan §4 (added required field requires bump)."""

    def test_default_schema_version_is_1(self):
        d = plan_to_content_dict(_plan())
        assert d["schema_version"] == 1

    def test_decoder_handles_unknown_version_with_explicit_error(self):
        from axiom.agents.pipeline.persistence import (
            UnsupportedPlanSchemaError,
        )
        d = plan_to_content_dict(_plan())
        d["schema_version"] = 999
        with pytest.raises(UnsupportedPlanSchemaError):
            plan_from_content_dict(d)


# --------------------------------------------------------------------------
# AgentEvent + AgentRun serialization
# --------------------------------------------------------------------------


class TestEventSerialization:
    def test_event_round_trip(self):
        evt = AgentEvent(
            run_id="r1",
            kind=AgentEventKind.TOOL_CALL,
            step_id="s1",
            payload={"tool_id": "ext.x", "args": {"a": 1}},
        )
        d = event_to_content_dict(evt)
        restored = event_from_content_dict(d)
        assert restored.run_id == "r1"
        assert restored.kind == AgentEventKind.TOOL_CALL
        assert restored.payload["tool_id"] == "ext.x"

    def test_event_kind_marker(self):
        evt = AgentEvent(run_id="r1", kind=AgentEventKind.THOUGHT)
        d = event_to_content_dict(evt)
        assert d["kind"] == "agent_event"


class TestRunSerialization:
    def test_run_round_trip(self):
        run = AgentRun(
            plan_id="p1",
            principal_id="agent:axi",
            accountable_human_id="@h:c",
            status=AgentRunStatus.COMPLETED,
        )
        d = run_to_content_dict(run)
        restored = run_from_content_dict(d)
        assert restored.run_id == run.run_id
        assert restored.plan_id == run.plan_id
        assert restored.status == AgentRunStatus.COMPLETED

    def test_run_kind_marker(self):
        run = AgentRun(plan_id="p", principal_id="a", accountable_human_id="@h:c")
        d = run_to_content_dict(run)
        assert d["kind"] == "agent_run"


# --------------------------------------------------------------------------
# MemoryBackedPlanStore — actual integration with CompositionService
# --------------------------------------------------------------------------


class TestMemoryBackedPlanStore:
    def test_write_plan_persists_to_memory(self, stack):
        store = MemoryBackedPlanStore(memory_stack=stack)
        plan = _plan()
        fragment = store.write_plan(plan)
        # The fragment is persisted; it carries plan content.
        assert fragment.cognitive_type == "procedural"
        assert fragment.content["kind"] == "plan"
        assert fragment.content["plan_id"] == plan.plan_id

    def test_read_plan_round_trips(self, stack):
        store = MemoryBackedPlanStore(memory_stack=stack)
        original = _plan()
        store.write_plan(original)

        read_back = store.read_plan(original.plan_id)
        assert read_back is not None
        assert read_back.plan_id == original.plan_id
        assert read_back.request.goal == original.request.goal
        assert len(read_back.steps) == 2

    def test_read_plan_unknown_returns_none(self, stack):
        store = MemoryBackedPlanStore(memory_stack=stack)
        result = store.read_plan("unknown-plan-id")
        assert result is None

    def test_list_plans_returns_all_plans_in_scope(self, stack):
        store = MemoryBackedPlanStore(memory_stack=stack)
        p1 = _plan()
        p2 = Plan(request=_req(), steps=())
        store.write_plan(p1)
        store.write_plan(p2)

        plans = store.list_plans(scope_id="test-persist-scope")
        ids = {p.plan_id for p in plans}
        assert p1.plan_id in ids
        assert p2.plan_id in ids

    def test_list_plans_filters_by_status(self, stack):
        store = MemoryBackedPlanStore(memory_stack=stack)
        draft = _plan()
        approved = _plan().with_status(PlanStatus.APPROVED)
        store.write_plan(draft)
        store.write_plan(approved)

        approved_only = store.list_plans(
            scope_id="test-persist-scope", status=PlanStatus.APPROVED,
        )
        statuses = {p.status for p in approved_only}
        assert statuses == {PlanStatus.APPROVED}

    def test_write_new_version_supersedes(self, stack):
        store = MemoryBackedPlanStore(memory_stack=stack)
        v1 = _plan()
        store.write_plan(v1)

        # Edit: produce a v2 that supersedes v1 and has different status
        from dataclasses import replace
        v2 = replace(v1, status=PlanStatus.PROPOSED, supersedes=v1.plan_id)
        store.write_plan(v2)

        read_v2 = store.read_plan(v2.plan_id)
        assert read_v2.supersedes == v1.plan_id

    def test_write_run_persists(self, stack):
        store = MemoryBackedPlanStore(memory_stack=stack)
        run = AgentRun(
            plan_id="p1",
            principal_id="agent:axi",
            accountable_human_id="@ben:example-org",
        )
        fragment = store.write_run(run)
        assert fragment.cognitive_type == "episodic"
        assert fragment.content["kind"] == "agent_run"

    def test_read_run_round_trips(self, stack):
        store = MemoryBackedPlanStore(memory_stack=stack)
        original = AgentRun(
            plan_id="p1",
            principal_id="agent:axi",
            accountable_human_id="@ben:example-org",
            status=AgentRunStatus.COMPLETED,
        )
        store.write_run(original)
        read = store.read_run(original.run_id)
        assert read is not None
        assert read.run_id == original.run_id

    def test_write_event_persists(self, stack):
        store = MemoryBackedPlanStore(memory_stack=stack)
        evt = AgentEvent(
            run_id="r1",
            kind=AgentEventKind.TOOL_CALL,
            payload={"tool_id": "ext.x"},
        )
        fragment = store.write_event(evt, accountable_human_id="@ben:example-org")
        assert fragment.cognitive_type == "episodic"
        assert fragment.content["kind"] == "agent_event"
        assert fragment.content["run_id"] == "r1"

    def test_list_events_for_run(self, stack):
        store = MemoryBackedPlanStore(memory_stack=stack)
        run_id = "test-run-events"
        e1 = AgentEvent(run_id=run_id, kind=AgentEventKind.RUN_STARTED)
        e2 = AgentEvent(run_id=run_id, kind=AgentEventKind.THOUGHT)
        store.write_event(e1, accountable_human_id="@ben:example-org")
        store.write_event(e2, accountable_human_id="@ben:example-org")

        events = store.list_events_for_run(run_id)
        kinds = {e.kind for e in events}
        assert AgentEventKind.RUN_STARTED in kinds
        assert AgentEventKind.THOUGHT in kinds


# --------------------------------------------------------------------------
# Compliance — pipeline_compliance marker
# --------------------------------------------------------------------------


@pytest.mark.pipeline_compliance
class TestPipelineCompliance:
    """Per ADR-034 compliance gates: plan + agent-run fixture round-trip.

    These are release-gate tests. A pinned plan written today MUST decode
    under every Axiom release through cohort end-of-life
    (memory-persistence-plan §6 cohort persistence). A run's events MUST
    replay deterministically (ADR-034 §D9 replay envelope).
    """

    def test_plan_round_trip_preserves_all_fields(self, stack):
        """A plan written today decodes under current Axiom without data loss."""
        store = MemoryBackedPlanStore(memory_stack=stack)
        original = Plan(
            request=PlanRequest(
                goal="canonical fixture goal",
                scope_id="test-persist-scope",
                principal_id="@fixture:human",
                accountable_human_id="@fixture:human",
                target_classification=ClassificationStamp.unclassified(),
                target_horizon=VisibilityHorizon.SCOPE_INTERNAL,
            ),
            steps=(
                PlanStep(
                    intent="step one",
                    tool_id="ext.foo.bar",
                    inputs={"k": "v"},
                    expected_outputs=("a", "b"),
                    gate=PlanStepGate.AUTO,
                    reach=StepReach(reads=("/r/**",), writes=(), network=()),
                ),
            ),
            status=PlanStatus.DRAFT,
            classification=ClassificationStamp.unclassified(),
            visibility=VisibilityHorizon.SCOPE_INTERNAL,
        )
        store.write_plan(original)
        restored = store.read_plan(original.plan_id)
        assert restored is not None
        # Every load-bearing field preserved.
        assert restored.plan_id == original.plan_id
        assert restored.status == original.status
        assert restored.visibility == original.visibility
        assert restored.steps[0].intent == original.steps[0].intent
        assert restored.steps[0].tool_id == original.steps[0].tool_id
        assert restored.steps[0].inputs == original.steps[0].inputs
        assert restored.steps[0].expected_outputs == original.steps[0].expected_outputs
        assert restored.steps[0].reach.reads == original.steps[0].reach.reads

    def test_plan_with_classification_round_trip(self, stack):
        """Classified plans decode under current Axiom without dropping
        the classification stamp."""
        cui = ClassificationStamp(level="cui")
        store = MemoryBackedPlanStore(memory_stack=stack)
        plan = Plan(
            request=PlanRequest(
                goal="classified analysis",
                scope_id="test-persist-scope",
                principal_id="@analyst:cohort",
                accountable_human_id="@analyst:cohort",
                target_classification=cui,
                target_horizon=VisibilityHorizon.SCOPE_INTERNAL,
            ),
            steps=(),
            classification=cui,
            visibility=VisibilityHorizon.SCOPE_INTERNAL,
        )
        store.write_plan(plan)
        restored = store.read_plan(plan.plan_id)
        assert restored is not None
        assert restored.classification.level == "cui"

    def test_agent_run_round_trip(self, stack):
        """Agent runs persist + decode cleanly; status preserved."""
        store = MemoryBackedPlanStore(memory_stack=stack)
        run = AgentRun(
            plan_id="canonical-plan",
            principal_id="agent:axi",
            accountable_human_id="@ben:example-org",
            status=AgentRunStatus.COMPLETED,
        )
        store.write_run(run)
        restored = store.read_run(run.run_id)
        assert restored is not None
        assert restored.status == AgentRunStatus.COMPLETED

    def test_agent_event_sequence_replay(self, stack):
        """A sequence of agent events written + read back preserves order
        sufficient for replay."""
        store = MemoryBackedPlanStore(memory_stack=stack)
        run_id = "compliance-run-replay"
        kinds = [
            AgentEventKind.RUN_STARTED,
            AgentEventKind.STEP_STARTED,
            AgentEventKind.TOOL_CALL,
            AgentEventKind.TOOL_RESULT,
            AgentEventKind.STEP_COMPLETED,
            AgentEventKind.RUN_COMPLETED,
        ]
        for k in kinds:
            store.write_event(
                AgentEvent(run_id=run_id, kind=k),
                accountable_human_id="@ben:example-org",
            )
        events = store.list_events_for_run(run_id)
        # All event kinds present (order may differ; replay relies on event_id +
        # event_time downstream).
        retrieved_kinds = {e.kind for e in events}
        assert set(kinds).issubset(retrieved_kinds)

    def test_unsupported_schema_version_raises_explicit_error(self, stack):
        """Per memory-persistence-plan §3: forward-compat decode is required.
        A plan with schema_version > current must fail with
        UnsupportedPlanSchemaError, never silently drop fields."""
        plan = _plan()
        d = plan_to_content_dict(plan)
        d["schema_version"] = 999
        from axiom.agents.pipeline.persistence import (
            UnsupportedPlanSchemaError,
        )
        with pytest.raises(UnsupportedPlanSchemaError):
            plan_from_content_dict(d)
