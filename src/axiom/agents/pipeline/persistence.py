# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Plan + AgentRun persistence — ADR-034 §D2 + §D3 + §D9.

Plans are MemoryFragments of cognitive_type=procedural; agent runs +
events are episodic. CompositionService.write is the single entry; the
ArtifactRegistry.find_fragments JSON1 path is the projection.

This module supplies:
- ``plan_to_content_dict`` / ``plan_from_content_dict`` — round-trip
  serialization for Plan inside MemoryFragment.content. Schema-versioned
  per memory-persistence-plan §4.
- ``run_to_content_dict`` / ``run_from_content_dict`` — same for AgentRun.
- ``event_to_content_dict`` / ``event_from_content_dict`` — same for AgentEvent.
- ``MemoryBackedPlanStore`` — concrete implementation that bridges
  PlanPipeline + AgentPipeline into the layered memory substrate.

Schema versioning: each content type carries its own ``schema_version`` field
(separate from MemoryFragment.provenance schema_version). Decoders dispatched
by ``schema_version`` per ``_PLAN_DECODERS`` / ``_RUN_DECODERS`` registries.
A future schema bump (e.g., adding a new field) registers a new decoder
without removing the prior — see memory-persistence-plan §3.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING, Any

from axiom.agents.pipeline.agent import (
    AgentEvent,
    AgentEventKind,
    AgentRun,
    AgentRunStatus,
)
from axiom.agents.pipeline.plan import (
    Plan,
    PlanRequest,
    PlanStatus,
    PlanStep,
    PlanStepGate,
    PlanStepStatus,
    StepOnError,
    StepReach,
)
from axiom.vega.federation.policy import (
    ClassificationStamp,
    VisibilityHorizon,
)

if TYPE_CHECKING:
    from axiom.memory.bootstrap import MemoryStack
    from axiom.memory.fragment import MemoryFragment


_CURRENT_PLAN_SCHEMA_VERSION = 1
_CURRENT_RUN_SCHEMA_VERSION = 1
_CURRENT_EVENT_SCHEMA_VERSION = 1


class UnsupportedPlanSchemaError(ValueError):
    """The plan's schema_version is newer than this Axiom version's decoders."""


# ---------------------------------------------------------------------------
# Helpers — encode / decode primitives
# ---------------------------------------------------------------------------


def _classification_to_dict(c: ClassificationStamp) -> dict:
    return {
        "level": c.level,
        # Keep encoding minimal in v1: extension fields land in v2 if needed.
    }


def _classification_from_dict(d: Mapping[str, Any]) -> ClassificationStamp:
    level = d.get("level", "unclassified")
    if level == "unclassified":
        return ClassificationStamp.unclassified()
    # Best-effort reconstruction; full ClassificationStamp v2 round-trip
    # is a follow-up that Stage 5b federation will need.
    return ClassificationStamp(level=level)


def _visibility_to_str(v: VisibilityHorizon) -> str:
    return v.value


def _visibility_from_str(s: str) -> VisibilityHorizon:
    return VisibilityHorizon(s)


def _reach_to_dict(r: StepReach) -> dict:
    return {
        "reads": list(r.reads),
        "writes": list(r.writes),
        "network": list(r.network),
    }


def _reach_from_dict(d: Mapping[str, Any]) -> StepReach:
    return StepReach(
        reads=tuple(d.get("reads") or ()),
        writes=tuple(d.get("writes") or ()),
        network=tuple(d.get("network") or ()),
    )


def _step_to_dict(s: PlanStep) -> dict:
    return {
        "step_id": s.step_id,
        "intent": s.intent,
        "tool_id": s.tool_id,
        "inputs": dict(s.inputs),
        "expected_outputs": list(s.expected_outputs),
        "gate": s.gate.value,
        "on_error": s.on_error.value,
        "status": s.status.value,
        "reach": _reach_to_dict(s.reach),
        "proof_artifacts": list(s.proof_artifacts),
        # proof + replay_envelope: stored as None in v1; tightened in v2.
    }


def _step_from_dict(d: Mapping[str, Any]) -> PlanStep:
    return PlanStep(
        intent=d["intent"],
        step_id=d["step_id"],
        tool_id=d.get("tool_id"),
        inputs=dict(d.get("inputs") or {}),
        expected_outputs=tuple(d.get("expected_outputs") or ()),
        gate=PlanStepGate(d.get("gate", "auto")),
        on_error=StepOnError(d.get("on_error", "abort")),
        status=PlanStepStatus(d.get("status", "pending")),
        reach=_reach_from_dict(d.get("reach") or {}),
        proof_artifacts=tuple(d.get("proof_artifacts") or ()),
    )


def _request_to_dict(r: PlanRequest) -> dict:
    return {
        "goal": r.goal,
        "scope_id": r.scope_id,
        "principal_id": r.principal_id,
        "accountable_human_id": r.accountable_human_id,
        "target_classification": _classification_to_dict(r.target_classification),
        "target_horizon": _visibility_to_str(r.target_horizon),
        "constraints": dict(r.constraints),
        "parent_plan_id": r.parent_plan_id,
        "model_strategy": r.model_strategy,
    }


def _request_from_dict(d: Mapping[str, Any]) -> PlanRequest:
    return PlanRequest(
        goal=d["goal"],
        scope_id=d["scope_id"],
        principal_id=d["principal_id"],
        accountable_human_id=d["accountable_human_id"],
        target_classification=_classification_from_dict(
            d.get("target_classification") or {}
        ),
        target_horizon=_visibility_from_str(
            d.get("target_horizon", "scope_internal")
        ),
        constraints=dict(d.get("constraints") or {}),
        parent_plan_id=d.get("parent_plan_id"),
        model_strategy=d.get("model_strategy"),
    )


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def plan_to_content_dict(plan: Plan) -> dict:
    """Serialize a Plan into a content dict suitable for MemoryFragment.content."""
    return {
        "kind": "plan",
        "schema_version": _CURRENT_PLAN_SCHEMA_VERSION,
        "plan_id": plan.plan_id,
        "request": _request_to_dict(plan.request),
        "steps": [_step_to_dict(s) for s in plan.steps],
        "status": plan.status.value,
        "classification": _classification_to_dict(plan.classification),
        "visibility": _visibility_to_str(plan.visibility),
        "supersedes": plan.supersedes,
        "derived_from": list(plan.derived_from),
        # replay_envelope: v1 stores envelope_id only; full envelope round-trip in v2.
        "replay_envelope_id": getattr(plan.replay_envelope, "envelope_id", None)
        if plan.replay_envelope is not None else None,
    }


def _decode_plan_v1(d: Mapping[str, Any]) -> Plan:
    return Plan(
        request=_request_from_dict(d["request"]),
        steps=tuple(_step_from_dict(s) for s in d.get("steps", [])),
        plan_id=d["plan_id"],
        status=PlanStatus(d.get("status", "draft")),
        classification=_classification_from_dict(d.get("classification") or {}),
        visibility=_visibility_from_str(d.get("visibility", "scope_internal")),
        supersedes=d.get("supersedes"),
        derived_from=tuple(d.get("derived_from") or ()),
    )


_PLAN_DECODERS = {
    1: _decode_plan_v1,
}


def plan_from_content_dict(d: Mapping[str, Any]) -> Plan:
    """Decode a Plan from MemoryFragment.content; schema-version dispatched."""
    version = d.get("schema_version", 1)
    decoder = _PLAN_DECODERS.get(version)
    if decoder is None:
        raise UnsupportedPlanSchemaError(
            f"plan schema_version={version} > {_CURRENT_PLAN_SCHEMA_VERSION}; "
            "upgrade Axiom to read this plan"
        )
    return decoder(d)


# ---------------------------------------------------------------------------
# AgentRun
# ---------------------------------------------------------------------------


def run_to_content_dict(run: AgentRun, *, event_time: str | None = None) -> dict:
    from datetime import datetime
    return {
        "kind": "agent_run",
        "schema_version": _CURRENT_RUN_SCHEMA_VERSION,
        "run_id": run.run_id,
        "plan_id": run.plan_id,
        "principal_id": run.principal_id,
        "accountable_human_id": run.accountable_human_id,
        "status": run.status.value,
        # event_time required for episodic content per fragment validator.
        "event_time": event_time or datetime.now(UTC).isoformat(),
        # Events stored as separate fragments (one per event); the run fragment
        # holds only the run-level snapshot. Listing events: list_events_for_run.
    }


def _decode_run_v1(d: Mapping[str, Any]) -> AgentRun:
    return AgentRun(
        plan_id=d["plan_id"],
        principal_id=d["principal_id"],
        accountable_human_id=d["accountable_human_id"],
        run_id=d["run_id"],
        status=AgentRunStatus(d.get("status", "initializing")),
    )


_RUN_DECODERS = {
    1: _decode_run_v1,
}


def run_from_content_dict(d: Mapping[str, Any]) -> AgentRun:
    version = d.get("schema_version", 1)
    decoder = _RUN_DECODERS.get(version)
    if decoder is None:
        raise UnsupportedPlanSchemaError(
            f"run schema_version={version} > {_CURRENT_RUN_SCHEMA_VERSION}"
        )
    return decoder(d)


# ---------------------------------------------------------------------------
# AgentEvent
# ---------------------------------------------------------------------------


def event_to_content_dict(event: AgentEvent, *, event_time: str | None = None) -> dict:
    from datetime import datetime
    return {
        "kind": "agent_event",
        "schema_version": _CURRENT_EVENT_SCHEMA_VERSION,
        "event_id": event.event_id,
        "run_id": event.run_id,
        "event_kind": event.kind.value,
        "step_id": event.step_id,
        "payload": dict(event.payload),
        # event_time required for episodic content per fragment validator.
        "event_time": event_time or datetime.now(UTC).isoformat(),
    }


def _decode_event_v1(d: Mapping[str, Any]) -> AgentEvent:
    return AgentEvent(
        run_id=d["run_id"],
        kind=AgentEventKind(d["event_kind"]),
        event_id=d["event_id"],
        step_id=d.get("step_id"),
        payload=dict(d.get("payload") or {}),
    )


_EVENT_DECODERS = {
    1: _decode_event_v1,
}


def event_from_content_dict(d: Mapping[str, Any]) -> AgentEvent:
    version = d.get("schema_version", 1)
    decoder = _EVENT_DECODERS.get(version)
    if decoder is None:
        raise UnsupportedPlanSchemaError(
            f"event schema_version={version} > {_CURRENT_EVENT_SCHEMA_VERSION}"
        )
    return decoder(d)


# ---------------------------------------------------------------------------
# Registry query helper — fast/slow path
# ---------------------------------------------------------------------------


def _find_fragments(
    registry,
    *,
    cognitive_type: str,
    content_kind: str,
    content_match_key: str | None = None,
    content_match_value: str | None = None,
) -> list:
    """Find fragments matching cognitive_type + content.kind.

    Uses the SQLiteBackend.find_fragments JSON1 fast path when available;
    falls back to iterating registry.list(kind="fragment") for any backend.
    """
    backend = getattr(registry, "_backend", None)
    fast = getattr(backend, "find_fragments", None)
    if fast is not None:
        # Use the SQL-side filter; we still post-filter on content_kind + match
        # because the SQL helper doesn't know about our content.kind convention.
        artifacts = fast(cognitive_type=cognitive_type)
    else:
        artifacts = registry.list(kind="fragment")

    out = []
    for a in artifacts:
        data = a.data
        if data.get("cognitive_type") != cognitive_type:
            continue
        content = data.get("content", {})
        if content.get("kind") != content_kind:
            continue
        if content_match_key is not None:
            if content.get(content_match_key) != content_match_value:
                continue
        out.append(a)
    return out


# ---------------------------------------------------------------------------
# MemoryBackedPlanStore — bridge to CompositionService + ArtifactRegistry
# ---------------------------------------------------------------------------


@dataclass
class MemoryBackedPlanStore:
    """Persistence bridge for Plan + AgentRun + AgentEvent.

    All writes flow through CompositionService (single-entry invariant per
    spec-memory §1). Reads use ArtifactRegistry.find_fragments JSON1 fast
    path keyed on cognitive_type + content.kind.
    """

    memory_stack: MemoryStack

    # ---- Plan ----

    def write_plan(self, plan: Plan) -> MemoryFragment:
        content = plan_to_content_dict(plan)
        return self.memory_stack.composition.write(
            content=content,
            cognitive_type="procedural",
            principal_id=plan.request.principal_id,
            agents=set(),
            resources=set(),
            accountable_human_id=plan.request.accountable_human_id,
        )

    def read_plan(self, plan_id: str) -> Plan | None:
        registry = self.memory_stack.composition.artifact_registry
        artifacts = _find_fragments(
            registry,
            cognitive_type="procedural",
            content_kind="plan",
            content_match_key="plan_id",
            content_match_value=plan_id,
        )
        # Multiple versions may share plan_id (supersedes chain). Pick the
        # most recent one by created_at if multiple.
        if not artifacts:
            return None
        # Artifact data is the fragment dict; its content carries the plan dict.
        artifacts.sort(key=lambda a: getattr(a, "created_at", "") or "")
        latest = artifacts[-1]
        frag_dict = latest.data
        content = frag_dict.get("content", {})
        if content.get("kind") != "plan":
            return None
        return plan_from_content_dict(content)

    def list_plans(
        self,
        *,
        scope_id: str,
        status: PlanStatus | None = None,
    ) -> Sequence[Plan]:
        registry = self.memory_stack.composition.artifact_registry
        artifacts = _find_fragments(
            registry,
            cognitive_type="procedural",
            content_kind="plan",
        )
        plans: list[Plan] = []
        for a in artifacts:
            content = a.data.get("content", {})
            if content.get("kind") != "plan":
                continue
            try:
                plan = plan_from_content_dict(content)
            except UnsupportedPlanSchemaError:
                continue
            if plan.request.scope_id != scope_id:
                continue
            if status is not None and plan.status != status:
                continue
            plans.append(plan)
        return tuple(plans)

    # ---- AgentRun ----

    def write_run(self, run: AgentRun) -> MemoryFragment:
        content = run_to_content_dict(run)
        return self.memory_stack.composition.write(
            content=content,
            cognitive_type="episodic",
            principal_id=run.principal_id,
            agents=set(),
            resources=set(),
            accountable_human_id=run.accountable_human_id,
        )

    def read_run(self, run_id: str) -> AgentRun | None:
        registry = self.memory_stack.composition.artifact_registry
        artifacts = _find_fragments(
            registry,
            cognitive_type="episodic",
            content_kind="agent_run",
            content_match_key="run_id",
            content_match_value=run_id,
        )
        for a in artifacts:
            content = a.data.get("content", {})
            return run_from_content_dict(content)
        return None

    # ---- AgentEvent ----

    def write_event(
        self, event: AgentEvent, *, accountable_human_id: str
    ) -> MemoryFragment:
        content = event_to_content_dict(event)
        return self.memory_stack.composition.write(
            content=content,
            cognitive_type="episodic",
            principal_id="agent:runtime",   # actor is the agent runtime; accountable
            agents=set(),
            resources=set(),
            accountable_human_id=accountable_human_id,
        )

    def list_events_for_run(self, run_id: str) -> Sequence[AgentEvent]:
        registry = self.memory_stack.composition.artifact_registry
        artifacts = _find_fragments(
            registry,
            cognitive_type="episodic",
            content_kind="agent_event",
            content_match_key="run_id",
            content_match_value=run_id,
        )
        events: list[AgentEvent] = []
        for a in artifacts:
            content = a.data.get("content", {})
            events.append(event_from_content_dict(content))
        return tuple(events)
