# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Layer 1 capability evals in recorded-model replay mode (ADR-232 part 9, E1).

The eval target is the capability and what it did, not a surface. A case calls
the dispatcher (:func:`axiom.infra.skill_dispatch.invoke_capability`) directly
with a fixed principal and fixture inputs, while a recorded transcript stands in
for the model. A run costs no tokens and cannot flake on the model.

Transcript fixture: JSON lines, one event per line.

``{"type": "model", "content": "...", "tool_calls": [{"name": "x", "args": {}}],
"usage": {"tokens": 120, "dollars": 0.0004}}``
    one model turn. ``tool_calls`` and ``usage`` are optional.

``{"type": "tool_result", "name": "x", "result": ...}``
    the result the named tool returned, consumed in order per tool name.

A capability under test reaches the recorded model through :func:`current_model`
(the live runtime that replaces it arrives with the agent loop, phase A3). The
harness then asserts, from the result and what the replay observed: the rung that
ran, whether the result satisfies ``output_schema``, the verify outcome, cost
against the declared budget, and the tool trace against the allow-list.

Not here, deliberately: live scheduled runs, replicates with confidence
intervals for continuous metrics (cost, latency), and a paired candidate-versus-
baseline comparison over the same cases. ``axiom.evals.significance`` has a paired
test for binary outcomes only. Recorded in ADR-232 open question 10.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult

__all__ = [
    "CapabilityCase",
    "CapabilityOutcome",
    "ReplayExhausted",
    "ReplayModel",
    "current_model",
    "load_transcript",
    "run_capability_case",
    "use_model",
]


class ReplayExhausted(RuntimeError):
    """The capability asked for more than the transcript recorded.

    A drift signal, not a retry: the capability now behaves differently from the
    run that was recorded, and the transcript has to be re-recorded on purpose.
    """


def load_transcript(source: str | Path) -> list[dict[str, Any]]:
    """Parse a JSON-lines transcript. Blank lines are ignored; a bad line names its number."""
    events: list[dict[str, Any]] = []
    for n, line in enumerate(Path(source).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{source}:{n}: not valid JSON ({exc.msg})") from exc
        if not isinstance(event, dict) or event.get("type") not in ("model", "tool_result"):
            raise ValueError(f"{source}:{n}: each line is an object with type 'model' or 'tool_result'")
        if event["type"] == "tool_result" and "name" not in event:
            raise ValueError(f"{source}:{n}: a tool_result needs a name")
        events.append(event)
    return events


class ReplayModel:
    """Plays a recorded transcript and keeps the trace of what was asked of it."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._turns = [e for e in events if e["type"] == "model"]
        self._results: dict[str, list[Any]] = {}
        for e in events:
            if e["type"] == "tool_result":
                self._results.setdefault(e["name"], []).append(e.get("result"))
        self.turns_used = 0
        self.tokens = 0
        self.dollars = 0.0
        self.tool_calls: list[str] = []

    def next_turn(self) -> dict[str, Any]:
        """The next recorded model turn; accrues its usage and notes its tool calls."""
        if self.turns_used >= len(self._turns):
            raise ReplayExhausted(f"transcript has {len(self._turns)} model turn(s); turn {self.turns_used + 1} requested")
        turn = self._turns[self.turns_used]
        self.turns_used += 1
        usage = turn.get("usage") or {}
        self.tokens += int(usage.get("tokens", 0))
        self.dollars += float(usage.get("dollars", 0.0))
        self.tool_calls.extend(str(c.get("name")) for c in turn.get("tool_calls") or ())
        return turn

    def tool_result(self, name: str) -> Any:
        """The next recorded result for tool ``name``."""
        queue = self._results.get(name)
        if not queue:
            raise ReplayExhausted(f"no recorded result left for tool {name!r}")
        return queue.pop(0)


_ACTIVE: contextvars.ContextVar[ReplayModel | None] = contextvars.ContextVar("capability_replay_model", default=None)


def current_model() -> ReplayModel:
    """The recorded model for this run. Raises outside :func:`use_model`."""
    model = _ACTIVE.get()
    if model is None:
        raise RuntimeError("no replay model is active; run the capability through run_capability_case")
    return model


@contextlib.contextmanager
def use_model(model: ReplayModel) -> Iterator[ReplayModel]:
    token = _ACTIVE.set(model)
    try:
        yield model
    finally:
        _ACTIVE.reset(token)


@dataclass
class CapabilityCase:
    """One golden case: a capability, its inputs, a transcript, and what must hold.

    ``expect`` keys (all optional): ``ok`` (bool), ``rung`` (``deterministic`` or
    ``agentic``, observed from whether the model was consulted), ``schema_valid``
    (bool), ``verify`` (``passed``, ``failed``, ``not_declared`` or ``n/a``),
    ``within_budget`` (bool), ``max_turns``, ``max_tokens``, ``max_dollars``,
    ``tools_within_allow_list`` (bool).
    """

    name: str
    capability: str
    params: dict[str, Any] = field(default_factory=dict)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    expect: dict[str, Any] = field(default_factory=dict)
    model_pin: str | None = None


@dataclass
class CapabilityOutcome:
    case: str
    ok: bool
    rung: str
    schema_valid: bool | None
    verify: str
    turns: int
    tokens: int
    dollars: float
    within_budget: bool | None
    tool_trace: list[str]
    tools_within_allow_list: bool
    errors: list[str]
    mismatches: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.mismatches


def _schema_valid(schema: Mapping[str, Any] | None, value: Any) -> bool | None:
    if schema is None:
        return None
    import jsonschema

    return jsonschema.Draft202012Validator(dict(schema)).is_valid(value)


def _verify(registry: SkillRegistry, ctx: SkillContext, name: str, spec_verify: str | None,
            waiver: str | None, result: SkillResult) -> str:
    # The dispatcher will run verify itself in phase A3; until then the harness
    # does, with the same rule: only a verifier that passes counts as passed.
    if spec_verify is None:
        return "n/a"
    if spec_verify == "not_declared":
        return "not_declared"
    from axiom.infra.skill_dispatch import invoke_capability

    verdict = invoke_capability(
        registry, spec_verify, {"capability": name, "result": result.value}, ctx,
        surface="eval", eventbus=None, hold_on_approval=False,
    )
    return "passed" if verdict.ok else "failed"


def run_capability_case(
    registry: SkillRegistry,
    case: CapabilityCase,
    *,
    ctx: SkillContext | None = None,
) -> CapabilityOutcome:
    """Run ``case`` through the dispatcher with the recorded model and compare to ``expect``."""
    from axiom.infra.skill_dispatch import invoke_capability

    spec = registry.spec(case.capability)
    if spec is None:
        raise KeyError(f"{case.capability!r} is not registered with a SkillSpec")
    if ctx is None:
        ctx = SkillContext(
            registry=registry,
            state_dir=Path(tempfile.mkdtemp(prefix="capability-eval-")),
            logger=logging.getLogger("capability-eval"),
        )
    model = ReplayModel(case.transcript)
    with use_model(model):
        result = invoke_capability(
            registry, case.capability, dict(case.params), ctx,
            surface="eval", eventbus=None, hold_on_approval=False,
        )

    agent = spec.agent
    budget = spec.budget or (agent.budget if agent else None)
    within = None
    if budget is not None and model.turns_used:
        within = all(
            limit is None or used <= limit
            for used, limit in ((model.turns_used, budget.turns), (model.tokens, budget.tokens),
                                (model.dollars, budget.dollars))
        )
    allow = set(agent.allowed_tools if agent else spec.allowed_tools)
    tools_ok = all(t in allow for t in model.tool_calls)
    verify_ref = spec.verify or (agent.verify if agent else None)
    waiver = agent.verify_waiver if agent else None
    outcome = CapabilityOutcome(
        case=case.name,
        ok=result.ok,
        rung="agentic" if model.turns_used else "deterministic",
        schema_valid=_schema_valid(agent.output_schema if agent else None, result.value) if result.ok else False,
        verify=_verify(registry, ctx, case.capability, verify_ref, waiver, result) if result.ok else "n/a",
        turns=model.turns_used,
        tokens=model.tokens,
        dollars=model.dollars,
        within_budget=within,
        tool_trace=list(model.tool_calls),
        tools_within_allow_list=tools_ok,
        errors=list(result.errors),
    )
    outcome.mismatches = _compare(outcome, case.expect)
    return outcome


def _compare(o: CapabilityOutcome, expect: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("ok", "rung", "schema_valid", "verify", "within_budget", "tools_within_allow_list"):
        if key in expect and getattr(o, key) != expect[key]:
            out.append(f"{key}: expected {expect[key]!r}, observed {getattr(o, key)!r}")
    for key, attr in (("max_turns", "turns"), ("max_tokens", "tokens"), ("max_dollars", "dollars")):
        if key in expect and getattr(o, attr) > expect[key]:
            out.append(f"{attr}: {getattr(o, attr)} exceeds {key} {expect[key]}")
    return out
