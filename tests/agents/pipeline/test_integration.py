# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""End-to-end integration: plan + agent + hooks + gates + proof + replay + sandbox.

This is the architectural coherence test for ADR-034 Phase 1. It exercises:

- PlanPipeline.derive(req) producing a Plan with proof + replay envelope.
- ApprovalGate auto-approving AUTO-gated steps.
- AgentPipeline.run(req) executing the plan steps as memory events.
- AgentHooks transforming events; PlanHooks validating + post-deriving.
- ProofSpec verification against step output via default_registry.
- ReplayEnvelope capturing the run's deterministic inputs.
- StepReach + classify_reach producing a sensible sandbox class.

It does NOT yet test:
- AskPipeline-driven derivation (Stage 2).
- AEOSToolRuntime tool invocation (Stage 2).
- Federation projection (Phase 4).
- Real CompositionService writes (depends on ADR-035 plumbing).
"""

from __future__ import annotations

from dataclasses import replace

from axiom.agents.pipeline.agent import (
    AgentEvent,
    AgentEventKind,
    AgentPipeline,
    AgentRun,
    AgentRunRequest,
    AgentRunStatus,
)
from axiom.agents.pipeline.gates import (
    ApprovalGate,
    ApprovalOutcome,
    GateContext,
    RaciAssignment,
)
from axiom.agents.pipeline.hooks import (
    apply_agent_hooks,
    apply_plan_hooks,
)
from axiom.agents.pipeline.plan import (
    Plan,
    PlanPipeline,
    PlanRequest,
    PlanStatus,
    PlanStep,
    PlanStepGate,
    StepReach,
)
from axiom.agents.pipeline.proof import (
    ProofSpec,
    ProofType,
    default_registry,
)
from axiom.agents.pipeline.replay import (
    EnvelopeBuilder,
    ReplayMode,
    capture_model_call,
    capture_retrieval,
    envelopes_equivalent,
)
from axiom.agents.pipeline.sandbox import (
    SandboxClass,
    classify_reach,
    summarize_reach,
)

# --------------------------------------------------------------------------
# A worked example: NE-101 student asks an agent to explain a concept.
# --------------------------------------------------------------------------


def _example_plan() -> Plan:
    """A two-step plan: retrieve + explain.

    Step 1 retrieves textbook chunks (read-only; null proof).
    Step 2 explains the concept with citations (retrieval-proof).
    """
    req = PlanRequest(
        goal="explain primary-loop differences",
        scope_id="classroom:ne101",
        principal_id="agent:axi",
        accountable_human_id="@student:example-org",
    )
    retrieve_step = PlanStep(
        intent="retrieve textbook chunks on primary loops",
        tool_id="rag.retrieve",
        inputs={"query": "primary loop comparison"},
        expected_outputs=("citations",),
        gate=PlanStepGate.AUTO,
        reach=StepReach(reads=("/cohort/textbooks/**",)),
        proof=ProofSpec(
            proof_type=ProofType.RETRIEVAL,
            parameters={"min_citations": 1, "min_score": 0.3, "recency_window_days": 365},
            description="at least one cited chunk with score >= 0.3",
        ),
    )
    explain_step = PlanStep(
        intent="explain primary-loop differences citing retrieved chunks",
        tool_id="llm.complete",
        inputs={"prompt": "compare primary loops"},
        expected_outputs=("answer",),
        gate=PlanStepGate.AUTO,
        reach=StepReach(network=("api.openai.com",)),
        proof=ProofSpec(
            proof_type=ProofType.NULL,
            parameters={"reason": "explanation is qualitative; null-proof accept-on-trust"},
            description="explanation accepted on trust",
        ),
    )
    return Plan(request=req, steps=(retrieve_step, explain_step))


class TestEndToEndPlanLifecycle:
    def test_plan_derivation_through_pipeline(self):
        derive_calls: list[PlanRequest] = []

        def derive_fn(req: PlanRequest) -> Plan:
            derive_calls.append(req)
            return _example_plan()

        pipeline = PlanPipeline(derive_fn=derive_fn)
        req = PlanRequest(
            goal="g",
            scope_id="classroom:ne101",
            principal_id="agent:axi",
            accountable_human_id="@student:example-org",
        )
        plan = pipeline.derive(req)
        assert len(derive_calls) == 1
        assert plan.status == PlanStatus.DRAFT
        assert len(plan.steps) == 2

    def test_plan_hooks_can_post_process(self):
        class PlanProposer:
            def post_derive(self, plan: Plan) -> Plan:
                return replace(plan, status=PlanStatus.PROPOSED)

        plan = _example_plan()
        proposed = apply_plan_hooks(PlanProposer(), "post_derive", plan)
        assert proposed.status == PlanStatus.PROPOSED

    def test_plan_hooks_validate(self):
        class StepCountValidator:
            def validate(self, plan: Plan) -> tuple[str, ...]:
                if len(plan.steps) < 1:
                    return ("plan needs at least one step",)
                return ()

        ok = apply_plan_hooks(StepCountValidator(), "validate", _example_plan())
        assert ok == ()

        empty = Plan(
            request=_example_plan().request,
            steps=(),
        )
        issues = apply_plan_hooks(StepCountValidator(), "validate", empty)
        assert issues == ("plan needs at least one step",)


class TestEndToEndApproval:
    def test_auto_gate_approves_both_steps(self):
        plan = _example_plan()
        gate = ApprovalGate()
        ctx = GateContext(
            accountable_human_id="@student:example-org",
            principal_id="agent:axi",
            scope_id="classroom:ne101",
            raci=RaciAssignment(
                responsible=("@student:example-org",),
                accountable=("@student:example-org",),
            ),
        )
        outcomes = [gate.evaluate(s, ctx).outcome for s in plan.steps]
        assert outcomes == [
            ApprovalOutcome.AUTO_APPROVED,
            ApprovalOutcome.AUTO_APPROVED,
        ]


class TestEndToEndReach:
    def test_classify_reach_for_each_step(self):
        plan = _example_plan()
        retrieve_class = classify_reach(plan.steps[0].reach)
        explain_class = classify_reach(plan.steps[1].reach)
        assert retrieve_class == SandboxClass.READ_ONLY
        assert explain_class == SandboxClass.EPHEMERAL_CONTAINER

    def test_summarize_reach_user_facing(self):
        plan = _example_plan()
        retrieve_summary = summarize_reach(plan.steps[0].reach)
        assert "reads" in retrieve_summary
        assert "container" not in retrieve_summary.lower()
        explain_summary = summarize_reach(plan.steps[1].reach)
        assert "network" in explain_summary


class TestEndToEndProof:
    def test_retrieval_proof_succeeds_with_citations(self):
        plan = _example_plan()
        retrieve_step = plan.steps[0]
        registry = default_registry()
        outputs = {
            "citations": [
                {"score": 0.9, "recency_days": 30},
                {"score": 0.6, "recency_days": 100},
            ]
        }
        result = registry.verify(retrieve_step.proof, outputs)
        assert result.success is True
        assert result.artifact is not None

    def test_retrieval_proof_fails_below_threshold(self):
        plan = _example_plan()
        retrieve_step = plan.steps[0]
        registry = default_registry()
        outputs = {
            "citations": [
                {"score": 0.1, "recency_days": 30},  # below min_score=0.3
            ]
        }
        result = registry.verify(retrieve_step.proof, outputs)
        assert result.success is False

    def test_null_proof_always_succeeds(self):
        plan = _example_plan()
        explain_step = plan.steps[1]
        registry = default_registry()
        result = registry.verify(explain_step.proof, {})
        assert result.success is True
        assert "null" in result.rationale.lower() or "trust" in result.rationale.lower()


class TestEndToEndAgentRun:
    def test_run_executes_each_step_emitting_events(self):
        plan = _example_plan()
        events_observed: list[AgentEvent] = []
        steps_remaining = list(plan.steps)

        def step_fn(run: AgentRun) -> AgentEvent | None:
            if not steps_remaining:
                return None
            current = steps_remaining.pop(0)
            return AgentEvent(
                run_id=run.run_id,
                kind=AgentEventKind.STEP_COMPLETED,
                step_id=current.step_id,
                payload={"intent": current.intent},
            )

        pipeline = AgentPipeline(step_fn=step_fn, on_event=events_observed.append)
        req = AgentRunRequest(
            plan_id=plan.plan_id,
            principal_id="agent:axi",
            accountable_human_id="@student:example-org",
        )
        run = pipeline.run(req)

        assert run.status == AgentRunStatus.COMPLETED
        # 1 RUN_STARTED + 2 step events + 1 RUN_COMPLETED
        assert len(run.events) == 4
        step_completed_events = [
            e for e in run.events if e.kind == AgentEventKind.STEP_COMPLETED
        ]
        assert len(step_completed_events) == 2
        assert step_completed_events[0].payload["intent"] == "retrieve textbook chunks on primary loops"

    def test_agent_hooks_can_transform_event(self):
        class TaggingHooks:
            def post_event(self, run: AgentRun, event: AgentEvent) -> AgentEvent:
                return replace(
                    event,
                    payload={**dict(event.payload), "cohort": "prague-summer-2026"},
                )

        run = AgentRun(plan_id="p", principal_id="a", accountable_human_id="@h:c")
        evt = AgentEvent(run_id=run.run_id, kind=AgentEventKind.THOUGHT)
        transformed = apply_agent_hooks(TaggingHooks(), "post_event", run, evt)
        assert transformed.payload["cohort"] == "prague-summer-2026"


class TestEndToEndReplay:
    def test_envelope_captures_step_inputs(self):
        builder = EnvelopeBuilder(mode=ReplayMode.BEST_EFFORT)
        capture_model_call(
            builder,
            provider="example-qwen",
            model="qwen-32b",
            temperature=0.0,
            system_prompt="explain like a textbook",
            user_prompt="compare PWR vs BWR primary loops",
        )
        capture_retrieval(
            builder,
            query="primary loop comparison",
            fragment_ids=["frag:1", "frag:2"],
            scores=[0.9, 0.6],
        )
        builder.declare_gap(
            "real_time_clock",
            "best-effort capture omits wall-clock-dependent inputs",
            "informational",
        )
        envelope = builder.build()

        assert envelope.mode == ReplayMode.BEST_EFFORT
        assert len(envelope.captured) >= 2
        assert len(envelope.not_captured) == 1
        assert envelope.fingerprint  # non-empty hex

    def test_two_replays_with_same_inputs_compare_equivalent(self):
        def make_envelope() -> ReplayEnvelope:  # noqa: F821
            b = EnvelopeBuilder(mode=ReplayMode.BEST_EFFORT)
            capture_model_call(
                b,
                provider="example-qwen",
                model="qwen-32b",
                temperature=0.0,
                system_prompt="x",
                user_prompt="y",
            )
            return b.build()

        e1 = make_envelope()
        e2 = make_envelope()
        ok, _diff = envelopes_equivalent(e1, e2)
        assert ok is True

    def test_diverging_envelopes_compare_inequivalent(self):
        b1 = EnvelopeBuilder()
        capture_model_call(
            b1, provider="x", model="m", temperature=0.0,
            system_prompt="s", user_prompt="u",
        )
        b2 = EnvelopeBuilder()
        capture_model_call(
            b2, provider="x", model="m", temperature=0.7,  # differs
            system_prompt="s", user_prompt="u",
        )
        ok, diff = envelopes_equivalent(b1.build(), b2.build())
        assert ok is False
        assert diff  # non-empty diagnostic


class TestArchitecturalCoherence:
    """The architectural-coherence test: a plan flows through every primitive
    in its intended order without contract gaps."""

    def test_plan_lifecycle_end_to_end(self):
        # 1. Derive
        plan = _example_plan()

        # 2. Validate via PlanHooks
        class Validator:
            def validate(self, plan: Plan) -> tuple[str, ...]:
                return ()

        issues = apply_plan_hooks(Validator(), "validate", plan)
        assert issues == ()

        # 3. Approve
        gate = ApprovalGate()
        ctx = GateContext(
            accountable_human_id="@student:example-org",
            principal_id="agent:axi",
            scope_id="classroom:ne101",
            raci=RaciAssignment(
                responsible=("@student:example-org",),
                accountable=("@student:example-org",),
            ),
        )
        for step in plan.steps:
            decision = gate.evaluate(step, ctx)
            assert decision.outcome == ApprovalOutcome.AUTO_APPROVED

        # 4. Sandbox-class each step's reach (audit aid).
        for step in plan.steps:
            sandbox_class = classify_reach(step.reach)
            assert sandbox_class != SandboxClass.NONE  # both have reach

        # 5. Run the agent.
        steps_remaining = list(plan.steps)

        def step_fn(run: AgentRun) -> AgentEvent | None:
            if not steps_remaining:
                return None
            current = steps_remaining.pop(0)
            return AgentEvent(
                run_id=run.run_id,
                kind=AgentEventKind.STEP_COMPLETED,
                step_id=current.step_id,
            )

        pipeline = AgentPipeline(step_fn=step_fn)
        req = AgentRunRequest(
            plan_id=plan.plan_id,
            principal_id="agent:axi",
            accountable_human_id="@student:example-org",
        )
        run = pipeline.run(req)
        assert run.status == AgentRunStatus.COMPLETED

        # 6. Verify each step's proof.
        registry = default_registry()
        for step in plan.steps:
            if step.proof.proof_type == ProofType.RETRIEVAL:
                # synthesize an output that satisfies the retrieval proof
                outputs = {"citations": [{"score": 0.9, "recency_days": 10}]}
            else:
                outputs = {}
            result = registry.verify(step.proof, outputs)
            assert result.success is True

        # 7. Capture the run's replay envelope.
        builder = EnvelopeBuilder()
        capture_model_call(
            builder,
            provider="example-qwen",
            model="qwen-32b",
            temperature=0.0,
            system_prompt="explain",
            user_prompt="compare loops",
        )
        envelope = builder.build()
        assert envelope.fingerprint
