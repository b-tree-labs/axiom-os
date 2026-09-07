# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.agents.pipeline.agent — AgentRun data shapes + pipeline shell."""

from __future__ import annotations

import pytest

from axiom.agents.pipeline.agent import (
    AgentEvent,
    AgentEventKind,
    AgentPipeline,
    AgentRun,
    AgentRunRequest,
    AgentRunStatus,
    InterruptPolicy,
)
from axiom.agents.pipeline.plan import Plan, PlanRequest


def _plan() -> Plan:
    req = PlanRequest(
        goal="g",
        scope_id="s",
        principal_id="p",
        accountable_human_id="@h:c",
    )
    return Plan(request=req, steps=())


class TestAgentEvent:
    def test_event_has_auto_id(self):
        a = AgentEvent(run_id="r1", kind=AgentEventKind.RUN_STARTED)
        b = AgentEvent(run_id="r1", kind=AgentEventKind.RUN_STARTED)
        assert a.event_id != b.event_id

    def test_event_kinds(self):
        for k in (
            AgentEventKind.RUN_STARTED,
            AgentEventKind.STEP_STARTED,
            AgentEventKind.THOUGHT,
            AgentEventKind.TOOL_CALL,
            AgentEventKind.TOOL_RESULT,
            AgentEventKind.STEP_COMPLETED,
            AgentEventKind.INTERRUPT_RECEIVED,
            AgentEventKind.RUN_COMPLETED,
            AgentEventKind.RUN_ABORTED,
            AgentEventKind.HANDOFF_TO_PEER,
        ):
            assert isinstance(k.value, str)

    def test_event_payload_optional(self):
        e = AgentEvent(run_id="r1", kind=AgentEventKind.THOUGHT)
        assert e.payload == {}
        e2 = AgentEvent(
            run_id="r1",
            kind=AgentEventKind.TOOL_CALL,
            payload={"tool_id": "ext.x", "args": {"a": 1}},
        )
        assert e2.payload["tool_id"] == "ext.x"

    def test_event_is_frozen(self):
        e = AgentEvent(run_id="r1", kind=AgentEventKind.THOUGHT)
        with pytest.raises((AttributeError, TypeError)):
            e.kind = AgentEventKind.TOOL_CALL  # type: ignore[misc]


class TestAgentRunRequest:
    def test_request_required_fields(self):
        req = AgentRunRequest(
            plan_id="p1",
            principal_id="agent:axi",
            accountable_human_id="@user:example-org",
        )
        assert req.plan_id == "p1"
        assert req.accountable_human_id == "@user:example-org"

    def test_request_defaults(self):
        req = AgentRunRequest(
            plan_id="p1",
            principal_id="agent:axi",
            accountable_human_id="@user:example-org",
        )
        assert req.max_steps == 100
        assert req.budget_usd is None
        assert req.interrupt_policy == InterruptPolicy.USER_SIGNAL_ONLY
        assert req.sandbox is None

    def test_request_is_frozen(self):
        req = AgentRunRequest(
            plan_id="p1", principal_id="agent:axi", accountable_human_id="@h:c"
        )
        with pytest.raises((AttributeError, TypeError)):
            req.plan_id = "p2"  # type: ignore[misc]


class TestAgentRun:
    def test_run_has_auto_id(self):
        a = AgentRun(plan_id="p1", principal_id="a", accountable_human_id="@h:c")
        b = AgentRun(plan_id="p1", principal_id="a", accountable_human_id="@h:c")
        assert a.run_id != b.run_id

    def test_run_default_status(self):
        run = AgentRun(plan_id="p1", principal_id="a", accountable_human_id="@h:c")
        assert run.status == AgentRunStatus.INITIALIZING

    def test_run_default_events_empty(self):
        run = AgentRun(plan_id="p1", principal_id="a", accountable_human_id="@h:c")
        assert run.events == ()

    def test_with_status(self):
        run = AgentRun(plan_id="p1", principal_id="a", accountable_human_id="@h:c")
        running = run.with_status(AgentRunStatus.RUNNING)
        assert run.status == AgentRunStatus.INITIALIZING
        assert running.status == AgentRunStatus.RUNNING
        assert running.run_id == run.run_id

    def test_append_event_returns_new_run(self):
        run = AgentRun(plan_id="p1", principal_id="a", accountable_human_id="@h:c")
        evt = AgentEvent(run_id=run.run_id, kind=AgentEventKind.RUN_STARTED)
        with_evt = run.append_event(evt)
        assert run.events == ()
        assert with_evt.events == (evt,)

    def test_append_event_validates_run_id(self):
        run = AgentRun(plan_id="p1", principal_id="a", accountable_human_id="@h:c")
        evt = AgentEvent(run_id="other", kind=AgentEventKind.THOUGHT)
        with pytest.raises(ValueError, match="run_id mismatch"):
            run.append_event(evt)

    def test_run_terminal_states(self):
        for s in (
            AgentRunStatus.COMPLETED,
            AgentRunStatus.ABORTED,
            AgentRunStatus.HANDOFF_TO_PEER,
            AgentRunStatus.FAILED_PROOF,
        ):
            run = AgentRun(
                plan_id="p", principal_id="a", accountable_human_id="@h:c", status=s
            )
            assert run.is_terminal()

    def test_run_non_terminal(self):
        for s in (
            AgentRunStatus.INITIALIZING,
            AgentRunStatus.RUNNING,
            AgentRunStatus.PAUSED_FOR_APPROVAL,
        ):
            run = AgentRun(
                plan_id="p", principal_id="a", accountable_human_id="@h:c", status=s
            )
            assert not run.is_terminal()


class TestAgentPipeline:
    def test_pipeline_runs_with_injected_step_fn(self):
        plan = _plan()
        events_emitted: list[AgentEvent] = []
        thought_emitted = [False]

        def step_fn(run: AgentRun) -> AgentEvent | None:
            if thought_emitted[0]:
                return None
            thought_emitted[0] = True
            return AgentEvent(run_id=run.run_id, kind=AgentEventKind.THOUGHT)

        pipeline = AgentPipeline(step_fn=step_fn, on_event=events_emitted.append)
        req = AgentRunRequest(
            plan_id=plan.plan_id,
            principal_id="agent:axi",
            accountable_human_id="@h:c",
        )
        run = pipeline.run(req)

        assert run.status == AgentRunStatus.COMPLETED
        assert len(run.events) == 3  # RUN_STARTED + THOUGHT + RUN_COMPLETED
        assert run.events[0].kind == AgentEventKind.RUN_STARTED
        assert run.events[1].kind == AgentEventKind.THOUGHT
        assert events_emitted[-1].kind == AgentEventKind.RUN_COMPLETED

    def test_pipeline_respects_max_steps(self):
        # step_fn always returns a new event; pipeline should bail on max_steps.
        def step_fn(run: AgentRun) -> AgentEvent:
            return AgentEvent(run_id=run.run_id, kind=AgentEventKind.THOUGHT)

        pipeline = AgentPipeline(step_fn=step_fn)
        req = AgentRunRequest(
            plan_id="p",
            principal_id="agent:axi",
            accountable_human_id="@h:c",
            max_steps=3,
        )
        run = pipeline.run(req)
        # 1 RUN_STARTED + 3 step events + 1 RUN_ABORTED-or-COMPLETED
        # Per design: max_steps caps loop iterations; status = ABORTED with reason "max_steps"
        assert run.status == AgentRunStatus.ABORTED
        thought_events = [e for e in run.events if e.kind == AgentEventKind.THOUGHT]
        assert len(thought_events) == 3

    def test_pipeline_writes_run_started_event(self):
        def step_fn(run: AgentRun) -> AgentEvent | None:
            return None

        pipeline = AgentPipeline(step_fn=step_fn)
        req = AgentRunRequest(
            plan_id="p", principal_id="a", accountable_human_id="@h:c"
        )
        run = pipeline.run(req)
        assert run.events[0].kind == AgentEventKind.RUN_STARTED
        assert run.events[0].run_id == run.run_id

    def test_pipeline_terminal_event_matches_status(self):
        def step_fn(run: AgentRun) -> AgentEvent | None:
            return None

        pipeline = AgentPipeline(step_fn=step_fn)
        req = AgentRunRequest(
            plan_id="p", principal_id="a", accountable_human_id="@h:c"
        )
        run = pipeline.run(req)
        # COMPLETED status pairs with a RUN_COMPLETED event as the last event.
        assert run.status == AgentRunStatus.COMPLETED
        assert run.events[-1].kind == AgentEventKind.RUN_COMPLETED
