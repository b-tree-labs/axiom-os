# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Wire PlanPipeline to AskPipeline — ADR-034 §D1.

Plan derivation reuses AskPipeline. This module supplies:

- ``parse_steps_from_response`` — tolerant JSON parser turning an LLM response
  into a tuple of ``PlanStep``.
- ``PlanDerivationHooks`` — ``AskHooks`` specialization biasing the system
  prompt toward structured plan output.
- ``AskBackedPlanPipeline`` — composes AskPipeline.ask + parser into a
  PlanPipeline-shaped derivation surface.

The output contract for the LLM is a JSON list of step records:

    [
      {"intent": "...", "tool_id": "...", "inputs": {...},
       "expected_outputs": [...], "gate": "auto|approve|manual",
       "reach": {"reads": [...], "writes": [...], "network": [...]}},
      ...
    ]

The parser is tolerant: it strips Markdown code fences, locates the first JSON
list in the response, and accepts unknown gate strings as ``AUTO`` (warning
behavior is up to the caller).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from axiom.agents.pipeline.plan import (
    Plan,
    PlanRequest,
    PlanStep,
    PlanStepGate,
    StepOnError,
    StepReach,
)

# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class PlanParseError(ValueError):
    """The LLM response did not contain parseable plan steps."""


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL | re.MULTILINE)
_LIST_RE = re.compile(r"\[\s*(?:\{.*?\}\s*,?\s*)*\]", re.DOTALL)


def _strip_fences(s: str) -> str:
    s = s.strip()
    m = _FENCE_RE.match(s)
    if m:
        return m.group(1).strip()
    # Fenced section embedded mid-response: try to extract.
    if s.startswith("```"):
        end = s.find("```", 3)
        if end > 3:
            inner = s[3:end]
            inner = inner.split("\n", 1)[1] if "\n" in inner else inner
            return inner.strip()
    return s


def _locate_json_list(s: str) -> str:
    """Return the first balanced JSON list substring; else the original."""
    m = _LIST_RE.search(s)
    if m:
        return m.group(0)
    return s


def _parse_gate(value: Any) -> PlanStepGate:
    if value is None:
        return PlanStepGate.AUTO
    if isinstance(value, str):
        try:
            return PlanStepGate(value)
        except ValueError:
            return PlanStepGate.AUTO
    return PlanStepGate.AUTO


def _parse_on_error(value: Any) -> StepOnError:
    if value is None:
        return StepOnError.ABORT
    if isinstance(value, str):
        try:
            return StepOnError(value)
        except ValueError:
            return StepOnError.ABORT
    return StepOnError.ABORT


def _parse_reach(value: Any) -> StepReach:
    if not isinstance(value, Mapping):
        return StepReach()
    return StepReach(
        reads=tuple(value.get("reads") or ()),
        writes=tuple(value.get("writes") or ()),
        network=tuple(value.get("network") or ()),
    )


def _parse_one_step(record: Mapping[str, Any]) -> PlanStep:
    if "intent" not in record or not record["intent"]:
        raise PlanParseError(f"step record missing 'intent': {record!r}")
    return PlanStep(
        intent=str(record["intent"]),
        tool_id=record.get("tool_id"),
        inputs=dict(record.get("inputs") or {}),
        expected_outputs=tuple(record.get("expected_outputs") or ()),
        gate=_parse_gate(record.get("gate")),
        on_error=_parse_on_error(record.get("on_error")),
        reach=_parse_reach(record.get("reach")),
    )


def parse_steps_from_response(response: str) -> tuple[PlanStep, ...]:
    """Tolerant LLM → PlanStep parser. Raises PlanParseError on malformed input."""
    if not response or not response.strip():
        raise PlanParseError("empty response")

    cleaned = _strip_fences(response)
    located = _locate_json_list(cleaned)

    try:
        data = json.loads(located)
    except json.JSONDecodeError as exc:
        raise PlanParseError(f"JSON decode failed: {exc}") from exc

    if not isinstance(data, list):
        raise PlanParseError(
            f"expected JSON list of step records, got {type(data).__name__}"
        )

    return tuple(_parse_one_step(record) for record in data)


# ---------------------------------------------------------------------------
# AskHooks specialization
# ---------------------------------------------------------------------------


_PLAN_DERIVATION_SYSTEM = """
You are a plan derivation agent. Given a user goal, emit a structured plan as
a JSON list of step records and nothing else.

Each step record must include:
  - "intent": short imperative description.
  - "tool_id" (optional): AEOS tool id; null if no tool needed.
  - "inputs" (optional): object of tool input args.
  - "expected_outputs" (optional): array of output names.
  - "gate" (optional): "auto" | "approve" | "manual"; default "auto".
  - "reach" (optional): {"reads": [...], "writes": [...], "network": [...]}.
  - "on_error" (optional): "abort" | "skip" | "ask"; default "abort".

Output ONLY the JSON list. No prose. No code fences.
""".strip()


class PlanDerivationHooks:
    """AskHooks that bias the LLM toward producing JSON plan-step output."""

    def contribute_layers(self, request, composer) -> None:  # type: ignore[no-untyped-def]
        # PromptComposer layers per axiom.infra.prompt_composer:
        # identity / capabilities / policies / domain_context / session_memory / retrieved / live.
        # Plan-derivation contract goes in "identity" (the system-persona slot).
        composer.add(
            layer="identity",
            name="plan_derivation_persona",
            content=_PLAN_DERIVATION_SYSTEM,
            source="plan_derivation_hooks",
        )

    def filter_citations(self, request, citations):  # type: ignore[no-untyped-def]
        return citations

    def pre_llm(self, request, composer, citations):  # type: ignore[no-untyped-def]
        return None

    def post_llm(self, request, raw_response, citations):  # type: ignore[no-untyped-def]
        return None


# ---------------------------------------------------------------------------
# AskBackedPlanPipeline
# ---------------------------------------------------------------------------


@dataclass
class AskBackedPlanPipeline:
    """PlanPipeline that derives via AskPipeline.

    The derivation flow:
    1. Build an AskRequest from the PlanRequest (goal as question).
    2. Run AskPipeline with PlanDerivationHooks contributing the
       structured-output system prompt.
    3. Parse the LLM response into PlanSteps.
    4. Return a Plan whose derived_from carries the citations the LLM saw.
    """

    ask_pipeline: Any  # AskPipeline; loose-typed to avoid memory<->agents import cycle
    hooks_factory: Any = None  # callable returning PlanDerivationHooks-shaped hooks

    def derive(self, request: PlanRequest) -> Plan:
        from axiom.memory.ask import AskRequest

        hooks = (self.hooks_factory or PlanDerivationHooks)()

        # Splice hooks into the ask pipeline. We avoid mutating the caller's
        # pipeline; instead, swap the hooks for the duration of this call.
        original_hooks = self.ask_pipeline.hooks
        self.ask_pipeline.hooks = hooks
        try:
            ask_request = AskRequest(
                question=request.goal,
                principal_id=request.principal_id,
                scope_id=request.scope_id,
                mode="plan_derivation",
                extra_context={"plan_request_goal": request.goal},
            )
            result = self.ask_pipeline.ask(ask_request)
        finally:
            self.ask_pipeline.hooks = original_hooks

        steps = parse_steps_from_response(result.answer)
        derived_from = tuple(c.source_id for c in result.citations)
        return Plan(
            request=request,
            steps=steps,
            derived_from=derived_from,
        )
