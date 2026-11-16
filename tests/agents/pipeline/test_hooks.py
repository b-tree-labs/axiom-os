# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.agents.pipeline.hooks — PlanHooks + AgentHooks protocols.

Per ADR-034 §D8: extensions specialize via hooks, never by forking the pipeline.
Hooks use the same getattr-resolved pattern as AskHooks (axiom.memory.ask) so
extensions implement only what they need.
"""

from __future__ import annotations

from axiom.agents.pipeline.agent import (
    AgentEvent,
    AgentEventKind,
    AgentRun,
)
from axiom.agents.pipeline.hooks import (
    AgentHooks,
    NullAgentHooks,
    NullPlanHooks,
    PlanHooks,
    apply_agent_hooks,
    apply_plan_hooks,
)
from axiom.agents.pipeline.plan import (
    Plan,
    PlanRequest,
    PlanStep,
)


def _req() -> PlanRequest:
    return PlanRequest(
        goal="g",
        scope_id="s",
        principal_id="p",
        accountable_human_id="@h:c",
    )


def _plan() -> Plan:
    return Plan(request=_req(), steps=(PlanStep(intent="x"),))


class TestNullPlanHooks:
    """Default no-op hooks pass everything through unchanged."""

    def test_pre_derive_passes_through(self):
        hooks = NullPlanHooks()
        req = _req()
        assert hooks.pre_derive(req) == req

    def test_post_derive_passes_through(self):
        hooks = NullPlanHooks()
        plan = _plan()
        assert hooks.post_derive(plan) == plan

    def test_validate_returns_empty(self):
        hooks = NullPlanHooks()
        plan = _plan()
        assert hooks.validate(plan) == ()


class TestNullAgentHooks:
    def test_pre_step_passes_through(self):
        hooks = NullAgentHooks()
        run = AgentRun(plan_id="p", principal_id="a", accountable_human_id="@h:c")
        assert hooks.pre_step(run) == run

    def test_post_event_returns_none(self):
        hooks = NullAgentHooks()
        run = AgentRun(plan_id="p", principal_id="a", accountable_human_id="@h:c")
        evt = AgentEvent(run_id=run.run_id, kind=AgentEventKind.THOUGHT)
        # Default: no override; returns None to indicate "no transformation".
        assert hooks.post_event(run, evt) is None

    def test_should_pause_returns_false(self):
        hooks = NullAgentHooks()
        run = AgentRun(plan_id="p", principal_id="a", accountable_human_id="@h:c")
        assert hooks.should_pause(run) is False


class TestApplyPlanHooks:
    """apply_plan_hooks supports getattr-resolved partial implementations.
    Mirrors AskHooks pattern in axiom.memory.ask."""

    def test_apply_pre_derive_with_partial_hooks(self):
        class PartialHooks:
            def pre_derive(self, req: PlanRequest) -> PlanRequest:
                from dataclasses import replace
                return replace(req, goal=req.goal + " [edited]")

        hooks = PartialHooks()
        req = _req()
        result = apply_plan_hooks(hooks, "pre_derive", req)
        assert result.goal == "g [edited]"

    def test_apply_post_derive_with_partial_hooks(self):
        from dataclasses import replace

        from axiom.agents.pipeline.plan import PlanStatus

        class PartialHooks:
            def post_derive(self, plan: Plan) -> Plan:
                return replace(plan, status=PlanStatus.PROPOSED)

        hooks = PartialHooks()
        plan = _plan()
        result = apply_plan_hooks(hooks, "post_derive", plan)
        assert result.status == PlanStatus.PROPOSED

    def test_apply_when_method_absent_returns_default(self):
        class EmptyHooks:
            pass

        hooks = EmptyHooks()
        req = _req()
        # absent method: default behavior is identity passthrough
        result = apply_plan_hooks(hooks, "pre_derive", req)
        assert result == req

    def test_apply_with_none_hooks(self):
        req = _req()
        result = apply_plan_hooks(None, "pre_derive", req)
        assert result == req

    def test_validate_aggregates(self):
        class HooksWithValidate:
            def validate(self, plan: Plan) -> tuple[str, ...]:
                return ("step count low",) if not plan.steps else ()

        empty_plan = Plan(request=_req(), steps=())
        hooks = HooksWithValidate()
        issues = apply_plan_hooks(hooks, "validate", empty_plan)
        assert "step count low" in issues


class TestApplyAgentHooks:
    def test_apply_pre_step_with_partial(self):
        class PartialHooks:
            def pre_step(self, run: AgentRun) -> AgentRun:
                # Add no events; return the run as-is. Just a contract check.
                return run

        hooks = PartialHooks()
        run = AgentRun(plan_id="p", principal_id="a", accountable_human_id="@h:c")
        result = apply_agent_hooks(hooks, "pre_step", run)
        assert result == run

    def test_apply_should_pause_returns_bool(self):
        class PartialHooks:
            def should_pause(self, run: AgentRun) -> bool:
                return len(run.events) > 5

        hooks = PartialHooks()
        run = AgentRun(plan_id="p", principal_id="a", accountable_human_id="@h:c")
        assert apply_agent_hooks(hooks, "should_pause", run) is False

    def test_apply_post_event_can_transform(self):
        from dataclasses import replace

        class PartialHooks:
            def post_event(self, run: AgentRun, event: AgentEvent) -> AgentEvent:
                return replace(event, payload={**dict(event.payload), "tagged": True})

        hooks = PartialHooks()
        run = AgentRun(plan_id="p", principal_id="a", accountable_human_id="@h:c")
        evt = AgentEvent(run_id=run.run_id, kind=AgentEventKind.THOUGHT)
        result = apply_agent_hooks(hooks, "post_event", run, evt)
        assert result.payload.get("tagged") is True

    def test_apply_with_none_hooks_pre_step(self):
        run = AgentRun(plan_id="p", principal_id="a", accountable_human_id="@h:c")
        # Without hooks, passthrough.
        result = apply_agent_hooks(None, "pre_step", run)
        assert result == run

    def test_apply_with_none_hooks_should_pause(self):
        run = AgentRun(plan_id="p", principal_id="a", accountable_human_id="@h:c")
        # Without hooks, never pause.
        assert apply_agent_hooks(None, "should_pause", run) is False


class TestProtocolAdherence:
    """Confirm the protocols are runtime-checkable for isinstance use."""

    def test_null_plan_hooks_is_plan_hooks(self):
        assert isinstance(NullPlanHooks(), PlanHooks)

    def test_null_agent_hooks_is_agent_hooks(self):
        assert isinstance(NullAgentHooks(), AgentHooks)
