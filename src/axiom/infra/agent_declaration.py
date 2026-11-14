# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""How a capability declares that a model may run (ADR-232 parts 1 to 4).

A capability is deterministic unless it says otherwise. This module holds the
vocabulary for saying otherwise, the :class:`AgentContract` an agent-backed
capability carries, and the check (:func:`check_declaration`) that ``axi ext lint``
and the registry lint both run, so there is one rule and one place it lives.

Nothing here runs a model. Declaring a capability ``agentic`` changes nothing at
call time in this phase; it makes the capability auditable, lintable and visible
in the generated SKILL.md, so the later phases (guard, ranking, record, verify)
have something declared to act on.

A declaration is plain data. The same checks run on a ``SkillSpec`` and on the
mapping a manifest carries, because lint reads manifests without importing
extension code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: How a capability may execute. ``None`` on a spec means "has not said", which
#: behaves as ``deterministic`` (ADR-185 convention).
EXECUTIONS = ("deterministic", "agentic", "hybrid")
AGENT_BACKED = ("agentic", "hybrid")

#: ADR-201 decision 9. The word "tier" is reserved for prompt tiers.
MODEL_CLASSES = ("simple", "standard", "smart", "smartest")

#: ``interactive`` under 5 s, ``bounded`` under 2 min, ``long`` needs a task handle.
LATENCY_CLASSES = ("interactive", "bounded", "long")

#: The placement vocabulary of ADR-201 decision 14.
RESIDENCIES = ("local_only", "site", "any")

BINDINGS = ("local", "remote")

#: The explicit way to say a capability has no deterministic check. It is
#: accepted only with a written waiver reason; absence is not a declaration.
VERIFY_NOT_DECLARED = "not_declared"

BUDGET_LIMITS = ("turns", "tokens", "dollars", "wall_clock_s")

#: Declaration keys a manifest or descriptor may carry, in render order.
DECLARATION_KEYS = (
    "execution",
    "model_class",
    "latency_class",
    "data_residency",
    "binding",
    "verify",
    "budget",
    "agent",
)

_VOCAB = {
    "execution": EXECUTIONS,
    "model_class": MODEL_CLASSES,
    "latency_class": LATENCY_CLASSES,
    "data_residency": RESIDENCIES,
    "binding": BINDINGS,
}


@dataclass(frozen=True)
class Budget:
    """The four limits on an agent run. There is no default for any of them.

    ``dollars`` is an estimate and is labelled as one wherever it is shown.
    A limit left as ``None`` is reported by lint as missing; a limit that is
    present but not a positive number is refused at construction.
    """

    turns: int | None = None
    tokens: int | None = None
    dollars: float | None = None
    wall_clock_s: float | None = None

    def __post_init__(self) -> None:
        for name in BUDGET_LIMITS:
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"budget.{name} must be a positive number, got {value!r}")
        for name in ("turns", "tokens"):
            value = getattr(self, name)
            if value is not None and int(value) != value:
                raise ValueError(f"budget.{name} must be a whole number, got {value!r}")

    @property
    def missing(self) -> tuple[str, ...]:
        """Limits not declared."""
        return tuple(n for n in BUDGET_LIMITS if getattr(self, n) is None)

    def as_dict(self) -> dict[str, Any]:
        return {n: getattr(self, n) for n in BUDGET_LIMITS if getattr(self, n) is not None}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> Budget:
        unknown = set(raw) - set(BUDGET_LIMITS)
        if unknown:
            raise ValueError(f"budget has unknown limits {sorted(unknown)}; known: {list(BUDGET_LIMITS)}")
        return cls(**{k: raw[k] for k in raw})


@dataclass(frozen=True)
class AgentContract:
    """What an agent-backed capability promises (ADR-232 part 1). Data, not code.

    - ``persona``: a persona from an extension manifest, the registry ``delegate()`` resolves.
    - ``allowed_tools``: tools the loop may call. Empty means no tools, not all tools.
    - ``model_class``: an ADR-201 class, ``simple`` to ``smartest``.
    - ``budget``: all four limits, no default.
    - ``output_schema``: JSON Schema of the typed result; a result that fails it fails.
    - ``verify``: the deterministic capability that checks a claimed success, or
      ``not_declared`` together with ``verify_waiver``.
    - ``stop_conditions``: named reasons the loop stops early, beyond the budget.

    Construction refuses malformed values. Completeness (a budget with all four
    limits, a verify step) is a lint finding rather than a constructor error, so
    an incomplete declaration can be loaded, reported and fixed.
    """

    persona: str = ""
    allowed_tools: tuple[str, ...] = ()
    model_class: str | None = None
    budget: Budget | None = None
    output_schema: dict[str, Any] | None = None
    verify: str | None = None
    verify_waiver: str | None = None
    stop_conditions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.model_class is not None and self.model_class not in MODEL_CLASSES:
            raise ValueError(f"agent.model_class must be one of {MODEL_CLASSES}, got {self.model_class!r}")
        if self.budget is not None and not isinstance(self.budget, Budget):
            raise TypeError("agent.budget must be a Budget")
        if self.output_schema is not None and not isinstance(self.output_schema, dict):
            raise TypeError("agent.output_schema must be a JSON Schema mapping")
        if self.output_schema is not None:
            try:
                import jsonschema

                jsonschema.Draft202012Validator.check_schema(self.output_schema)
            except Exception as exc:  # noqa: BLE001 - report any schema defect uniformly
                raise ValueError(f"agent.output_schema is not a valid JSON Schema: {exc}") from exc
        for attr in ("allowed_tools", "stop_conditions"):
            value = getattr(self, attr)
            if not isinstance(value, tuple) or not all(isinstance(v, str) and v for v in value):
                raise TypeError(f"agent.{attr} must be a tuple of non-empty strings")

    def as_dict(self) -> dict[str, Any]:
        """Plain data for a manifest or descriptor. Only what is declared."""
        out: dict[str, Any] = {"persona": self.persona, "allowed_tools": list(self.allowed_tools)}
        if self.model_class is not None:
            out["model_class"] = self.model_class
        if self.budget is not None:
            out["budget"] = self.budget.as_dict()
        if self.output_schema is not None:
            out["output_schema"] = self.output_schema
        if self.verify is not None:
            out["verify"] = self.verify
        if self.verify_waiver is not None:
            out["verify_waiver"] = self.verify_waiver
        if self.stop_conditions:
            out["stop_conditions"] = list(self.stop_conditions)
        return out

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> AgentContract:
        known = {"persona", "allowed_tools", "model_class", "budget", "output_schema",
                 "verify", "verify_waiver", "stop_conditions"}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"agent has unknown keys {sorted(unknown)}")
        budget = raw.get("budget")
        return cls(
            persona=str(raw.get("persona", "")),
            allowed_tools=tuple(raw.get("allowed_tools", ())),
            model_class=raw.get("model_class"),
            budget=Budget.from_mapping(budget) if isinstance(budget, Mapping) else None,
            output_schema=dict(raw["output_schema"]) if raw.get("output_schema") is not None else None,
            verify=raw.get("verify"),
            verify_waiver=raw.get("verify_waiver"),
            stop_conditions=tuple(raw.get("stop_conditions", ())),
        )


def validate_vocabulary(name: str, **declared: Any) -> None:
    """Raise ``ValueError`` for a declared value outside its closed vocabulary."""
    for key, allowed in _VOCAB.items():
        value = declared.get(key)
        if value is not None and value not in allowed:
            raise ValueError(f"{name}: {key} must be one of {allowed}, got {value!r}")


# ---------------------------------------------------------------- lint


@dataclass(frozen=True)
class AgentFinding:
    """One lint finding. ``code`` is stable; ``severity`` is ``error`` or ``warning``."""

    code: str
    severity: str
    skill: str
    message: str
    fix: str


@dataclass
class _View:
    """The fields the check reads, from either a spec or a mapping."""

    name: str
    execution: str | None
    risk: str | None
    model_class: str | None
    binding: str | None
    budget: Mapping[str, Any] | None
    verify: str | None
    verify_waiver: str | None
    agent: Mapping[str, Any] | None


def declaration_from_spec(spec: Any) -> dict[str, Any]:
    """The agent-declaration descriptor of a ``SkillSpec``; empty when nothing is declared."""
    out: dict[str, Any] = {}
    for key in ("execution", "model_class", "latency_class", "data_residency", "binding", "verify"):
        value = getattr(spec, key, None)
        if value is not None:
            out[key] = value
    budget = getattr(spec, "budget", None)
    if budget is not None:
        out["budget"] = budget.as_dict()
    agent = getattr(spec, "agent", None)
    if agent is not None:
        out["agent"] = agent.as_dict()
    return out


def _view(name: str, decl: Mapping[str, Any], risk: str | None) -> _View:
    agent = decl.get("agent") if isinstance(decl.get("agent"), Mapping) else None
    return _View(
        name=name,
        execution=decl.get("execution"),
        risk=risk,
        model_class=decl.get("model_class") or (agent or {}).get("model_class"),
        binding=decl.get("binding"),
        budget=decl.get("budget") if isinstance(decl.get("budget"), Mapping) else (agent or {}).get("budget"),
        verify=decl.get("verify") or (agent or {}).get("verify"),
        verify_waiver=(agent or {}).get("verify_waiver") or decl.get("verify_waiver"),
        agent=agent,
    )


def check_declaration(
    name: str,
    decl: Mapping[str, Any],
    *,
    risk: str | None = None,
    verifier_execution: Any = None,
) -> list[AgentFinding]:
    """Findings for one capability's agent declaration.

    ``decl`` is the descriptor (:func:`declaration_from_spec`, or the same keys from
    a manifest). A capability that declares nothing yields no findings, which is
    the guarantee that undeclared capabilities behave exactly as before.

    ``verifier_execution`` is an optional ``callable(name) -> str | None | KeyError``
    that returns the ``execution`` of a registered capability (``"unregistered"``
    when absent), used to confirm a ``verify`` reference resolves to a deterministic
    verifier. Without it the reference is not resolved (static manifest lint).

    Rules:

    - ``AEOS160`` agentic or hybrid without an ``agent`` contract.
    - ``AEOS161`` agent-backed without all four budget limits.
    - ``AEOS162`` agent-backed with no ``verify``, or ``not_declared`` without a waiver.
    - ``AEOS163`` the contract is missing persona, model class or output schema.
    - ``AEOS164`` ``verify`` names a capability that is unregistered or is itself agent-backed.
    - ``AEOS165`` agent fields declared on a capability that is not agent-backed.
    - ``AEOS166`` ``binding = "remote"`` without a ``verify`` (a returned artifact is verified locally).
    """
    if not decl:
        return []
    v = _view(name, decl, risk)
    out: list[AgentFinding] = []

    def add(code: str, message: str, fix: str, severity: str = "error") -> None:
        out.append(AgentFinding(code, severity, name, message, fix))

    backed = v.execution in AGENT_BACKED
    if not backed:
        if v.agent is not None or v.budget is not None:
            add("AEOS165",
                f"{name} declares an agent contract or budget but execution is {v.execution or 'undeclared'}",
                "set execution = \"agentic\" or \"hybrid\", or remove the agent and budget declarations")
        return out

    if v.agent is None:
        add("AEOS160", f"{name} is {v.execution} but declares no agent contract",
            "add agent = AgentContract(persona=..., model_class=..., budget=..., output_schema=..., verify=...)")
    else:
        for key, label in (("persona", "persona"), ("output_schema", "output schema")):
            if not v.agent.get(key):
                add("AEOS163", f"{name}: the agent contract has no {label}", f"declare agent.{key}")
        if not v.model_class:
            add("AEOS163", f"{name}: the agent contract has no model class",
                f"declare model_class, one of {list(MODEL_CLASSES)}")

    budget = dict(v.budget or {})
    missing = [n for n in BUDGET_LIMITS if budget.get(n) is None]
    if missing:
        add("AEOS161", f"{name} is {v.execution} and its budget is missing {', '.join(missing)}",
            "declare all four limits: turns, tokens, dollars (an estimate), wall_clock_s. There is no default")

    verify = v.verify
    if verify is None or verify == "":
        add("AEOS162", f"{name} is {v.execution} and declares no verify step",
            "set verify to a deterministic verifier capability, or verify = \"not_declared\" with agent.verify_waiver "
            "giving the reason")
    elif verify == VERIFY_NOT_DECLARED:
        if not (v.verify_waiver or "").strip():
            add("AEOS162", f"{name} sets verify = not_declared without a waiver",
                "give agent.verify_waiver a reason, or name a verifier capability")
        elif v.risk not in (None, "read"):
            add("AEOS162", f"{name} has risk {v.risk} and cannot waive verify",
                "a capability that is not read must name a verifier capability", )
    elif verifier_execution is not None:
        found = verifier_execution(verify)
        if found == "unregistered":
            add("AEOS164", f"{name}: verify names {verify!r}, which is not a registered capability",
                "register the verifier or correct the name")
        elif found in AGENT_BACKED:
            add("AEOS164", f"{name}: verify names {verify!r}, which is itself {found}; a verifier must be deterministic",
                "point verify at a deterministic capability")

    if v.binding == "remote" and verify in (None, "", VERIFY_NOT_DECLARED):
        add("AEOS166", f"{name} is bound remote without a verifier for the returned artifact",
            "a remote result is verified locally; name a verifier capability")
    return out


def render_execution_section(decl: Mapping[str, Any]) -> str:
    """The SKILL.md ``Execution`` section for a declaration (ADR-063: SKILL.md is generated).

    A reader, human or harness, sees that a model may run, what it may spend, and
    what checks the result, before calling.
    """
    lines = ["## Execution", ""]
    lines.append(f"- Execution: {decl.get('execution', 'deterministic')} (a model may run when this is called)"
                 if decl.get("execution") in AGENT_BACKED else
                 f"- Execution: {decl.get('execution', 'deterministic')}")
    agent = decl.get("agent") or {}
    model_class = decl.get("model_class") or agent.get("model_class")
    if model_class:
        lines.append(f"- Model class: {model_class}")
    for key, label in (("latency_class", "Latency class"), ("data_residency", "Data residency"),
                       ("binding", "Binding")):
        if decl.get(key):
            lines.append(f"- {label}: {decl[key]}")
    budget = decl.get("budget") or agent.get("budget") or {}
    if budget:
        parts = [f"{n} {budget[n]}" + (" (estimate)" if n == "dollars" else "")
                 for n in BUDGET_LIMITS if n in budget]
        lines.append("- Budget: " + ", ".join(parts))
    verify = decl.get("verify") or agent.get("verify")
    if verify == VERIFY_NOT_DECLARED:
        lines.append(f"- Verify: not declared (waiver: {agent.get('verify_waiver', 'none given')})")
    elif verify:
        lines.append(f"- Verify: `{verify}`")
    if agent.get("persona"):
        lines.append(f"- Persona: {agent['persona']}")
    lines.append("- Tools: " + (", ".join(agent.get("allowed_tools") or ()) or "none"))
    if agent.get("stop_conditions"):
        lines.append("- Stops early on: " + ", ".join(agent["stop_conditions"]))
    return "\n".join(lines)
