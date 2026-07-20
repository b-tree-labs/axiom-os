# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.agents.pipeline.gates — approval gate + RACI integration."""

from __future__ import annotations

import pytest

from axiom.agents.pipeline.gates import (
    ApprovalDecision,
    ApprovalGate,
    ApprovalOutcome,
    GateContext,
    RaciAssignment,
    RaciRole,
    approval_required,
    auto_gate,
    manual_gate,
)
from axiom.agents.pipeline.plan import (
    PlanStep,
    PlanStepGate,
)


def _step(gate: PlanStepGate = PlanStepGate.AUTO) -> PlanStep:
    return PlanStep(intent="x", gate=gate)


def _ctx(approver: str = "@ben:example-org") -> GateContext:
    return GateContext(
        accountable_human_id=approver,
        principal_id=approver,
        scope_id="s",
        raci=RaciAssignment(
            responsible=("@ben:example-org",),
            accountable=("@ben:example-org",),
            consulted=(),
            informed=(),
        ),
    )


class TestRaciAssignment:
    def test_assignment_fields(self):
        raci = RaciAssignment(
            responsible=("@a:c",),
            accountable=("@b:c",),
            consulted=("@c:c",),
            informed=("@d:c",),
        )
        assert raci.responsible == ("@a:c",)
        assert raci.accountable == ("@b:c",)

    def test_principal_has_role(self):
        raci = RaciAssignment(
            responsible=("@a:c",),
            accountable=("@b:c",),
        )
        assert raci.principal_has_role("@a:c", RaciRole.RESPONSIBLE)
        assert raci.principal_has_role("@b:c", RaciRole.ACCOUNTABLE)
        assert not raci.principal_has_role("@a:c", RaciRole.ACCOUNTABLE)
        assert not raci.principal_has_role("@x:c", RaciRole.RESPONSIBLE)

    def test_assignment_frozen(self):
        raci = RaciAssignment()
        with pytest.raises((AttributeError, TypeError)):
            raci.responsible = ("@x:c",)  # type: ignore[misc]


class TestApprovalDecision:
    def test_decision_has_auto_id(self):
        a = ApprovalDecision(
            step_id="s1", outcome=ApprovalOutcome.APPROVED, principal_id="@p:c"
        )
        b = ApprovalDecision(
            step_id="s1", outcome=ApprovalOutcome.APPROVED, principal_id="@p:c"
        )
        assert a.decision_id != b.decision_id

    def test_outcome_values(self):
        for o in (
            ApprovalOutcome.APPROVED,
            ApprovalOutcome.REJECTED,
            ApprovalOutcome.PENDING,
            ApprovalOutcome.AUTO_APPROVED,
            ApprovalOutcome.EXPIRED,
        ):
            assert isinstance(o.value, str)


class TestAutoGate:
    def test_auto_gate_step_returns_auto_approved(self):
        step = _step(PlanStepGate.AUTO)
        ctx = _ctx()
        decision = auto_gate(step, ctx)
        assert decision.outcome == ApprovalOutcome.AUTO_APPROVED
        assert decision.step_id == step.step_id

    def test_auto_gate_does_not_apply_to_approve_step(self):
        step = _step(PlanStepGate.APPROVE)
        ctx = _ctx()
        decision = auto_gate(step, ctx)
        # auto_gate refuses to auto-approve a step that explicitly requires approval
        assert decision.outcome == ApprovalOutcome.PENDING
        assert "explicit approval" in decision.rationale.lower() or "approve" in decision.rationale.lower()


class TestManualGate:
    def test_manual_gate_records_principal(self):
        step = _step(PlanStepGate.APPROVE)
        # Build a ctx where @reviewer is RESPONSIBLE.
        ctx = GateContext(
            accountable_human_id="@reviewer:example-org",
            principal_id="@reviewer:example-org",
            scope_id="s",
            raci=RaciAssignment(responsible=("@reviewer:example-org",)),
        )
        decision = manual_gate(
            step, ctx, approver="@reviewer:example-org", outcome=ApprovalOutcome.APPROVED,
            rationale="LGTM",
        )
        assert decision.outcome == ApprovalOutcome.APPROVED
        assert decision.principal_id == "@reviewer:example-org"
        assert decision.rationale == "LGTM"

    def test_manual_gate_rejects_unauthorized_approver(self):
        step = _step(PlanStepGate.APPROVE)
        ctx = _ctx("@ben:example-org")  # only ben is RESPONSIBLE/ACCOUNTABLE
        decision = manual_gate(
            step, ctx, approver="@stranger:elsewhere", outcome=ApprovalOutcome.APPROVED,
            rationale="ok",
        )
        # unauthorized approver: gate refuses; outcome rejected with rationale
        assert decision.outcome == ApprovalOutcome.REJECTED
        assert "not authorized" in decision.rationale.lower() or "unauthorized" in decision.rationale.lower()


class TestApprovalRequired:
    def test_auto_step_does_not_require_approval(self):
        step = _step(PlanStepGate.AUTO)
        assert approval_required(step) is False

    def test_approve_step_requires_approval(self):
        step = _step(PlanStepGate.APPROVE)
        assert approval_required(step) is True

    def test_manual_step_requires_approval(self):
        step = _step(PlanStepGate.MANUAL)
        assert approval_required(step) is True


class TestApprovalGate:
    """ApprovalGate is the higher-level coordinator binding step gates + RACI + decisions."""

    def test_evaluate_auto_step(self):
        gate = ApprovalGate()
        step = _step(PlanStepGate.AUTO)
        ctx = _ctx()
        decision = gate.evaluate(step, ctx)
        assert decision.outcome == ApprovalOutcome.AUTO_APPROVED

    def test_evaluate_approve_step_returns_pending(self):
        gate = ApprovalGate()
        step = _step(PlanStepGate.APPROVE)
        ctx = _ctx()
        decision = gate.evaluate(step, ctx)
        # No manual decision yet; outcome=PENDING signals "awaiting approval"
        assert decision.outcome == ApprovalOutcome.PENDING

    def test_evaluate_with_recorded_approval(self):
        gate = ApprovalGate()
        step = _step(PlanStepGate.APPROVE)
        ctx = _ctx()

        # Record an approval first.
        approved = manual_gate(
            step, ctx, approver="@ben:example-org",
            outcome=ApprovalOutcome.APPROVED, rationale="ok",
        )
        gate.record(approved)

        decision = gate.evaluate(step, ctx)
        assert decision.outcome == ApprovalOutcome.APPROVED

    def test_evaluate_with_recorded_rejection(self):
        gate = ApprovalGate()
        step = _step(PlanStepGate.APPROVE)
        ctx = _ctx()

        rejected = manual_gate(
            step, ctx, approver="@ben:example-org",
            outcome=ApprovalOutcome.REJECTED, rationale="risky",
        )
        gate.record(rejected)

        decision = gate.evaluate(step, ctx)
        assert decision.outcome == ApprovalOutcome.REJECTED

    def test_decisions_for_step(self):
        gate = ApprovalGate()
        step = _step(PlanStepGate.APPROVE)
        ctx = _ctx()

        gate.record(manual_gate(
            step, ctx, approver="@ben:example-org",
            outcome=ApprovalOutcome.APPROVED, rationale="v1",
        ))
        decisions = gate.decisions_for_step(step.step_id)
        assert len(decisions) == 1
        assert decisions[0].rationale == "v1"
