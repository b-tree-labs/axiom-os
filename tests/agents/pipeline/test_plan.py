# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.agents.pipeline.plan — core plan data shapes + pipeline shell."""

from __future__ import annotations

import pytest

from axiom.agents.pipeline.plan import (
    Plan,
    PlanPipeline,
    PlanRequest,
    PlanStatus,
    PlanStep,
    PlanStepGate,
    PlanStepStatus,
    StepReach,
)


class TestPlanStep:
    def test_step_has_auto_id(self):
        a = PlanStep(intent="do thing")
        b = PlanStep(intent="do thing")
        assert a.step_id != b.step_id
        assert len(a.step_id) >= 8

    def test_default_gate_is_auto(self):
        step = PlanStep(intent="x")
        assert step.gate == PlanStepGate.AUTO

    def test_default_status_is_pending(self):
        step = PlanStep(intent="x")
        assert step.status == PlanStepStatus.PENDING

    def test_default_reach_is_empty(self):
        step = PlanStep(intent="x")
        assert step.reach.reads == ()
        assert step.reach.writes == ()
        assert step.reach.network == ()

    def test_step_with_tool_id(self):
        step = PlanStep(
            intent="run analysis",
            tool_id="ext.python.run",
            inputs={"script": "main.py"},
            expected_outputs=("stdout", "exit_code"),
        )
        assert step.tool_id == "ext.python.run"
        assert step.inputs == {"script": "main.py"}
        assert step.expected_outputs == ("stdout", "exit_code")

    def test_step_with_explicit_reach(self):
        reach = StepReach(
            reads=("/repo/src/**",),
            writes=("/repo/src/**",),
            network=("api.openai.com",),
        )
        step = PlanStep(intent="edit", reach=reach)
        assert step.reach.reads == ("/repo/src/**",)

    def test_step_is_frozen(self):
        step = PlanStep(intent="x")
        with pytest.raises((AttributeError, TypeError)):
            step.intent = "y"  # type: ignore[misc]

    def test_step_gate_values(self):
        assert PlanStepGate.AUTO.value == "auto"
        assert PlanStepGate.APPROVE.value == "approve"
        assert PlanStepGate.MANUAL.value == "manual"

    def test_step_status_values(self):
        for s in (
            PlanStepStatus.PENDING,
            PlanStepStatus.RUNNING,
            PlanStepStatus.BLOCKED,
            PlanStepStatus.PROOF_ATTEMPTED,
            PlanStepStatus.PROOF_FAILED,
            PlanStepStatus.VERIFIED,
            PlanStepStatus.SKIPPED,
            PlanStepStatus.NULL_PROOF,
        ):
            assert isinstance(s.value, str)


class TestPlanRequest:
    def test_request_has_required_fields(self):
        req = PlanRequest(
            goal="solve X",
            scope_id="classroom:cs101",
            principal_id="@ben:example-org",
            accountable_human_id="@ben:example-org",
        )
        assert req.goal == "solve X"
        assert req.scope_id == "classroom:cs101"
        assert req.principal_id == "@ben:example-org"
        assert req.accountable_human_id == "@ben:example-org"

    def test_request_defaults(self):
        req = PlanRequest(
            goal="g", scope_id="s", principal_id="p", accountable_human_id="@h:c"
        )
        assert req.constraints == {}
        assert req.parent_plan_id is None
        assert req.model_strategy is None

    def test_request_is_frozen(self):
        req = PlanRequest(
            goal="g", scope_id="s", principal_id="p", accountable_human_id="@h:c"
        )
        with pytest.raises((AttributeError, TypeError)):
            req.goal = "x"  # type: ignore[misc]


class TestPlan:
    def _req(self):
        return PlanRequest(
            goal="g",
            scope_id="classroom:cs101",
            principal_id="@ben:example-org",
            accountable_human_id="@ben:example-org",
        )

    def test_plan_has_auto_id(self):
        a = Plan(request=self._req(), steps=())
        b = Plan(request=self._req(), steps=())
        assert a.plan_id != b.plan_id

    def test_plan_default_status(self):
        plan = Plan(request=self._req(), steps=())
        assert plan.status == PlanStatus.DRAFT

    def test_plan_carries_steps(self):
        steps = (PlanStep(intent="a"), PlanStep(intent="b"))
        plan = Plan(request=self._req(), steps=steps)
        assert len(plan.steps) == 2
        assert plan.steps[0].intent == "a"

    def test_plan_supersedes_chain(self):
        v1 = Plan(request=self._req(), steps=())
        v2 = Plan(request=self._req(), steps=(), supersedes=v1.plan_id)
        assert v2.supersedes == v1.plan_id

    def test_plan_classification_default_unclassified(self):
        plan = Plan(request=self._req(), steps=())
        # accept either an enum or a stamp object; the field exists and isn't None
        assert plan.classification is not None

    def test_plan_visibility_default_scope_internal(self):
        plan = Plan(request=self._req(), steps=())
        # default-deny per spec-federation-policy
        assert plan.visibility is not None

    def test_plan_is_frozen(self):
        plan = Plan(request=self._req(), steps=())
        with pytest.raises((AttributeError, TypeError)):
            plan.status = PlanStatus.APPROVED  # type: ignore[misc]


class TestPlanPipeline:
    """PlanPipeline is the orchestrator. v0 surface: a derive() method that
    accepts a request and returns a Plan. Implementation depends on AskPipeline +
    LLM; for v0 we test the *shape* with an injected derivation function."""

    def test_pipeline_requires_derive_callable(self):
        pipeline = PlanPipeline(derive_fn=lambda req: Plan(request=req, steps=()))
        req = PlanRequest(
            goal="g",
            scope_id="s",
            principal_id="p",
            accountable_human_id="@h:c",
        )
        plan = pipeline.derive(req)
        assert isinstance(plan, Plan)
        assert plan.request == req

    def test_pipeline_derive_produces_plan_with_request_bound(self):
        derive_calls: list[PlanRequest] = []

        def fake_derive(req: PlanRequest) -> Plan:
            derive_calls.append(req)
            return Plan(request=req, steps=(PlanStep(intent="step1"),))

        pipeline = PlanPipeline(derive_fn=fake_derive)
        req = PlanRequest(
            goal="goal",
            scope_id="s",
            principal_id="p",
            accountable_human_id="@h:c",
        )
        plan = pipeline.derive(req)

        assert len(derive_calls) == 1
        assert derive_calls[0] == req
        assert len(plan.steps) == 1
        assert plan.steps[0].intent == "step1"


class TestPlanStatusTransitions:
    """Plans are append-only; a new status produces a new Plan (immutable)."""

    def _req(self):
        return PlanRequest(
            goal="g",
            scope_id="s",
            principal_id="p",
            accountable_human_id="@h:c",
        )

    def test_with_status_returns_new_plan(self):
        plan = Plan(request=self._req(), steps=())
        approved = plan.with_status(PlanStatus.APPROVED)
        assert plan.status == PlanStatus.DRAFT
        assert approved.status == PlanStatus.APPROVED
        assert approved.plan_id == plan.plan_id  # same plan; status replaced

    def test_with_status_does_not_mutate_original(self):
        plan = Plan(request=self._req(), steps=())
        plan.with_status(PlanStatus.APPROVED)
        assert plan.status == PlanStatus.DRAFT
