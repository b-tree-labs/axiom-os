# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Approval gates + RACI integration — ADR-034 §D6.

A step's PlanStepGate (AUTO / APPROVE / MANUAL) declares the intent.
This module supplies the *runtime* that evaluates gates against RACI
(Responsible / Accountable / Consulted / Informed) assignments and records
human decisions.

Surface:
- ``RaciRole`` / ``RaciAssignment`` — who can approve.
- ``GateContext`` — runtime context for gate evaluation (accountable human,
  principal, scope, RACI).
- ``ApprovalOutcome`` enum + ``ApprovalDecision`` — recorded decisions.
- ``auto_gate(step, ctx)`` / ``manual_gate(step, ctx, ...)`` — primitive evaluators.
- ``approval_required(step)`` — gate predicate.
- ``ApprovalGate`` — coordinator; aggregates decisions + provides ``evaluate``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum

from axiom.agents.pipeline.plan import PlanStep, PlanStepGate

# ---------------------------------------------------------------------------
# RACI
# ---------------------------------------------------------------------------


class RaciRole(str, Enum):
    RESPONSIBLE = "responsible"
    ACCOUNTABLE = "accountable"
    CONSULTED = "consulted"
    INFORMED = "informed"


@dataclass(frozen=True)
class RaciAssignment:
    responsible: tuple[str, ...] = ()
    accountable: tuple[str, ...] = ()
    consulted: tuple[str, ...] = ()
    informed: tuple[str, ...] = ()

    def principal_has_role(self, principal_id: str, role: RaciRole) -> bool:
        bucket = {
            RaciRole.RESPONSIBLE: self.responsible,
            RaciRole.ACCOUNTABLE: self.accountable,
            RaciRole.CONSULTED: self.consulted,
            RaciRole.INFORMED: self.informed,
        }[role]
        return principal_id in bucket

    def can_approve(self, principal_id: str) -> bool:
        """Authorization predicate: R or A roles can approve a step."""
        return (
            self.principal_has_role(principal_id, RaciRole.RESPONSIBLE)
            or self.principal_has_role(principal_id, RaciRole.ACCOUNTABLE)
        )


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


class ApprovalOutcome(str, Enum):
    AUTO_APPROVED = "auto_approved"
    APPROVED = "approved"
    REJECTED = "rejected"
    PENDING = "pending"
    EXPIRED = "expired"


def _gen_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class ApprovalDecision:
    step_id: str
    outcome: ApprovalOutcome
    principal_id: str
    decision_id: str = field(default_factory=_gen_id)
    rationale: str = ""


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateContext:
    accountable_human_id: str
    principal_id: str
    scope_id: str
    raci: RaciAssignment = field(default_factory=RaciAssignment)


# ---------------------------------------------------------------------------
# Primitive gate evaluators
# ---------------------------------------------------------------------------


def approval_required(step: PlanStep) -> bool:
    return step.gate in (PlanStepGate.APPROVE, PlanStepGate.MANUAL)


def auto_gate(step: PlanStep, ctx: GateContext) -> ApprovalDecision:
    """Auto-approve gate; refuses to auto-approve a step that requires explicit approval."""
    if approval_required(step):
        return ApprovalDecision(
            step_id=step.step_id,
            outcome=ApprovalOutcome.PENDING,
            principal_id=ctx.principal_id,
            rationale=f"step requires explicit approval (gate={step.gate.value})",
        )
    return ApprovalDecision(
        step_id=step.step_id,
        outcome=ApprovalOutcome.AUTO_APPROVED,
        principal_id=ctx.principal_id,
        rationale=f"auto-approved per step gate={step.gate.value}",
    )


def manual_gate(
    step: PlanStep,
    ctx: GateContext,
    *,
    approver: str,
    outcome: ApprovalOutcome,
    rationale: str = "",
) -> ApprovalDecision:
    """Record a manual approval/rejection by a named principal.

    Refuses if approver is not R or A in the step's RACI assignment.
    """
    if not ctx.raci.can_approve(approver):
        return ApprovalDecision(
            step_id=step.step_id,
            outcome=ApprovalOutcome.REJECTED,
            principal_id=approver,
            rationale=f"approver {approver!r} not authorized (not R or A)",
        )
    return ApprovalDecision(
        step_id=step.step_id,
        outcome=outcome,
        principal_id=approver,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# ApprovalGate coordinator
# ---------------------------------------------------------------------------


@dataclass
class ApprovalGate:
    """Aggregates recorded decisions; evaluates a step's gate state.

    For auto-gated steps, evaluation auto-approves on first call.
    For approval-gated steps, evaluation returns PENDING until a decision is
    recorded; once recorded, the most recent decision drives the outcome.
    """

    _decisions: dict[str, list[ApprovalDecision]] = field(default_factory=dict)

    def record(self, decision: ApprovalDecision) -> None:
        self._decisions.setdefault(decision.step_id, []).append(decision)

    def decisions_for_step(self, step_id: str) -> tuple[ApprovalDecision, ...]:
        return tuple(self._decisions.get(step_id, ()))

    def evaluate(self, step: PlanStep, ctx: GateContext) -> ApprovalDecision:
        existing = self.decisions_for_step(step.step_id)
        if existing:
            # Most recent decision wins (append-only chain).
            return existing[-1]
        if not approval_required(step):
            return auto_gate(step, ctx)
        return ApprovalDecision(
            step_id=step.step_id,
            outcome=ApprovalOutcome.PENDING,
            principal_id=ctx.principal_id,
            rationale="awaiting approval",
        )
