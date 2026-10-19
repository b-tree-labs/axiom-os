# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Plan data shapes + PlanPipeline shell — ADR-034 §D2.

A Plan is a MemoryFragment of cognitive_type=procedural. This module defines
the in-memory Python shape; the bridge to MemoryFragment lands in a follow-up
once ADR-035's accountable-human plumbing settles.

Stage 1 surface (this module):
- PlanRequest / PlanStep / Plan dataclasses (frozen, append-only semantics).
- StepReach for the AEOS reach-vocabulary from analysis §10.2.
- Enums: PlanStatus, PlanStepStatus, PlanStepGate.
- PlanPipeline with an injected derive_fn — the LLM-driven derivation lands
  via AskPipeline integration in a follow-up.

Stage 2 (follow-up): PlanPipeline derives via AskPipeline; ProofSpec +
ReplayEnvelope are fully typed (parallel tracks deliver those modules).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from axiom.agents.pipeline.proof import ProofSpec
from axiom.agents.pipeline.replay import ReplayEnvelope
from axiom.vega.federation.policy import (
    ClassificationStamp,
    VisibilityHorizon,
)

# ---------------------------------------------------------------------------
# Step shape
# ---------------------------------------------------------------------------


class PlanStepGate(str, Enum):
    """Approval gate for a step (ADR-034 §D6)."""

    AUTO = "auto"
    APPROVE = "approve"
    MANUAL = "manual"


class PlanStepStatus(str, Enum):
    """Step lifecycle (ADR-034 §D2 + analysis §7.6)."""

    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    PROOF_ATTEMPTED = "proof_attempted"
    PROOF_FAILED = "proof_failed"
    VERIFIED = "verified"
    SKIPPED = "skipped"
    NULL_PROOF = "null_proof"


class StepOnError(str, Enum):
    ABORT = "abort"
    SKIP = "skip"
    ASK = "ask"


@dataclass(frozen=True)
class StepReach:
    """Declared reach for a step's tool invocation (analysis §10.2).

    The sandbox is the enforcer; this is the contract. Reach is what the user
    sees + approves. A tool that escapes its declared reach hard-fails with audit.
    """

    reads: tuple[str, ...] = ()      # filesystem path globs
    writes: tuple[str, ...] = ()     # filesystem path globs
    network: tuple[str, ...] = ()    # hostnames or "none"


def _gen_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class PlanStep:
    intent: str
    step_id: str = field(default_factory=_gen_id)
    tool_id: str | None = None
    inputs: Mapping[str, Any] = field(default_factory=dict)
    expected_outputs: tuple[str, ...] = ()
    gate: PlanStepGate = PlanStepGate.AUTO
    on_error: StepOnError = StepOnError.ABORT
    status: PlanStepStatus = PlanStepStatus.PENDING
    reach: StepReach = field(default_factory=StepReach)
    proof: ProofSpec | None = None
    proof_artifacts: tuple[str, ...] = ()    # fragment IDs of proof evidence

    def with_status(self, status: PlanStepStatus) -> PlanStep:
        return replace(self, status=status)

    def with_proof_artifacts(self, *artifact_ids: str) -> PlanStep:
        return replace(self, proof_artifacts=tuple(artifact_ids))


# ---------------------------------------------------------------------------
# Plan request + plan
# ---------------------------------------------------------------------------


class PlanStatus(str, Enum):
    """Plan lifecycle (ADR-034 §D2; append-only — transitions create new versions)."""

    DRAFT = "draft"
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    EXECUTING = "executing"
    COMPLETED = "completed"
    ABORTED = "aborted"


@dataclass(frozen=True)
class PlanRequest:
    """Input to PlanPipeline.derive."""

    goal: str
    scope_id: str
    principal_id: str                                # actor (may be agent)
    accountable_human_id: str                        # ADR-035; mandatory
    target_classification: ClassificationStamp = field(
        default_factory=ClassificationStamp.unclassified
    )
    target_horizon: VisibilityHorizon = VisibilityHorizon.SCOPE_INTERNAL
    constraints: Mapping[str, Any] = field(default_factory=dict)
    parent_plan_id: str | None = None
    model_strategy: str | None = None             # named strategy per spec-model-routing §13


@dataclass(frozen=True)
class Plan:
    request: PlanRequest
    steps: tuple[PlanStep, ...]
    plan_id: str = field(default_factory=_gen_id)
    status: PlanStatus = PlanStatus.DRAFT
    classification: ClassificationStamp = field(
        default_factory=ClassificationStamp.unclassified
    )
    visibility: VisibilityHorizon = VisibilityHorizon.SCOPE_INTERNAL
    supersedes: str | None = None                  # prior plan_id when this is a new version
    replay_envelope: ReplayEnvelope | None = None
    derived_from: tuple[str, ...] = ()                # fragment IDs in evidence

    def with_status(self, status: PlanStatus) -> Plan:
        return replace(self, status=status)

    def with_steps(self, steps: Sequence[PlanStep]) -> Plan:
        return replace(self, steps=tuple(steps))

    def step_by_id(self, step_id: str) -> PlanStep | None:
        for s in self.steps:
            if s.step_id == step_id:
                return s
        return None


# ---------------------------------------------------------------------------
# PlanPipeline — orchestrator shell
# ---------------------------------------------------------------------------


DeriveFn = Callable[[PlanRequest], Plan]


@dataclass
class PlanPipeline:
    """Shell pipeline; full AskPipeline integration follows in Stage 2.

    The injected ``derive_fn`` is the seam for testing and for incremental
    integration. Once the AskPipeline-driven derivation lands, the default
    factory will compose retrieval + composition + LLM into a derive_fn
    transparently — call sites won't change.
    """

    derive_fn: DeriveFn

    def derive(self, request: PlanRequest) -> Plan:
        return self.derive_fn(request)
