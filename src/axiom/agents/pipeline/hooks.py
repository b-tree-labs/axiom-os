# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PlanHooks + AgentHooks protocols — ADR-034 §D8.

Extensions specialize the plan + agent pipelines via these hooks rather than
by forking the pipeline class. The pattern mirrors ``axiom.memory.ask.AskHooks``
(getattr-resolved per-method) so extensions implement only what they need.

Two surfaces:

- ``PlanHooks`` — pre_derive (mutate the request before plan derivation),
  post_derive (transform the derived plan), validate (return per-plan issues).
- ``AgentHooks`` — pre_step (run-level transform before each step),
  post_event (per-event transform), should_pause (HITL pause decision).

Both protocols are ``@runtime_checkable`` so callers can ``isinstance(hooks, PlanHooks)``
to confirm conformance.

The ``apply_plan_hooks`` and ``apply_agent_hooks`` helpers do the
getattr-resolved dispatch: if the named method is absent, a sensible default
fires (identity passthrough; empty-tuple for validate; False for should_pause).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from axiom.agents.pipeline.agent import AgentEvent, AgentRun
    from axiom.agents.pipeline.plan import Plan, PlanRequest


# ---------------------------------------------------------------------------
# PlanHooks
# ---------------------------------------------------------------------------


@runtime_checkable
class PlanHooks(Protocol):
    def pre_derive(self, request: PlanRequest) -> PlanRequest: ...
    def post_derive(self, plan: Plan) -> Plan: ...
    def validate(self, plan: Plan) -> tuple[str, ...]: ...


class NullPlanHooks:
    """Default no-op hooks; identity passthrough."""

    def pre_derive(self, request: PlanRequest) -> PlanRequest:
        return request

    def post_derive(self, plan: Plan) -> Plan:
        return plan

    def validate(self, plan: Plan) -> tuple[str, ...]:
        return ()


# ---------------------------------------------------------------------------
# AgentHooks
# ---------------------------------------------------------------------------


@runtime_checkable
class AgentHooks(Protocol):
    def pre_step(self, run: AgentRun) -> AgentRun: ...
    def post_event(
        self, run: AgentRun, event: AgentEvent
    ) -> AgentEvent | None: ...
    def should_pause(self, run: AgentRun) -> bool: ...


class NullAgentHooks:
    """Default no-op hooks."""

    def pre_step(self, run: AgentRun) -> AgentRun:
        return run

    def post_event(
        self, run: AgentRun, event: AgentEvent
    ) -> AgentEvent | None:
        return None

    def should_pause(self, run: AgentRun) -> bool:
        return False


# ---------------------------------------------------------------------------
# Dispatch helpers — getattr-resolved per-method
# ---------------------------------------------------------------------------


_PLAN_DEFAULTS: dict[str, Any] = {
    "pre_derive": lambda req: req,
    "post_derive": lambda plan: plan,
    "validate": lambda plan: (),
}

_AGENT_DEFAULTS: dict[str, Any] = {
    "pre_step": lambda run: run,
    "post_event": lambda run, event: None,
    "should_pause": lambda run: False,
}


def apply_plan_hooks(hooks: Any, method: str, *args: Any) -> Any:
    """Resolve+invoke a PlanHooks method; fall back to default if absent or hooks is None."""
    if hooks is None:
        return _PLAN_DEFAULTS[method](*args)
    fn = getattr(hooks, method, None)
    if fn is None:
        return _PLAN_DEFAULTS[method](*args)
    return fn(*args)


def apply_agent_hooks(hooks: Any, method: str, *args: Any) -> Any:
    """Resolve+invoke an AgentHooks method; fall back to default if absent or hooks is None."""
    if hooks is None:
        return _AGENT_DEFAULTS[method](*args)
    fn = getattr(hooks, method, None)
    if fn is None:
        return _AGENT_DEFAULTS[method](*args)
    return fn(*args)
