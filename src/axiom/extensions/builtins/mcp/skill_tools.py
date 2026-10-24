# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Registry-driven MCP tool surface (ADR-073).

The MCP tool surface for extension capabilities derives from the
``SkillRegistry`` via the shared projector (``axiom.infra.capability_projection``)
— not a parallel manifest convention. One capability → one CLI verb, one MCP
tool, one agent tool, all with the same name, schema, and approval semantics.

- **Name:** ``axiom_`` server-prefix + the projector's surface name. This
  reproduces the prior ``axiom_{ns}__{verb}`` convention exactly, so MCP
  clients see no rename. The prefix is a transport concern (global tool-name
  uniqueness across MCP servers); the ``.``⇄``__`` mapping is the projector's.
- **Schema:** ``inputs_to_json_schema(spec.inputs)`` — shared with CLI/agent-tool.
- **Side-effects and risk:** the capability's declaration → MCP
  ``readOnlyHint`` / ``destructiveHint`` / ``idempotentHint`` annotations
  (:func:`tool_annotations`); chaining declarations → ``_meta["ai.axiom/chain"]``
  (:func:`chain_meta`).
- **Dispatch:** ``axiom.infra.skill_dispatch.invoke_capability``, the same
  chokepoint the CLI verb uses, so the ``tool.pre_invoke`` chain (and the
  GUARD consult on it) sees an MCP call exactly as it sees a CLI or chat
  one. The gateway is handed the *capability* name, never the mangled MCP
  tool name: one site rule on ``tool://press.draft`` covers every surface.
- **Exposure:** a capability is an MCP tool iff it opts in via
  ``surfaces=[…"mcp"]`` (AEOS §4.9.4) — the bounded-exposure guard.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from mcp.types import Tool, ToolAnnotations

from axiom.extensions.builtins.mcp.identity_gate import DEFAULT_PATTERNS
from axiom.infra.capability_projection import (
    capability_to_surface_name,
    inputs_to_json_schema,
    is_read_only,
    surface_to_capability_name,
)
from axiom.infra.skill_dispatch import MCP_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext, SkillRegistry, SkillSpec

# Server-scoped prefix for global MCP tool-name uniqueness.
MCP_PREFIX = "axiom_"

# (arguments) -> result dict, the shape server.dispatch_call expects.
MCPHandler = Callable[[dict], Awaitable[dict]]


def mcp_tool_name(capability: str) -> str:
    """``press.draft`` → ``axiom_press__draft`` (prefix + projector surface name)."""
    return MCP_PREFIX + capability_to_surface_name(capability)


def mcp_name_to_capability(tool_name: str) -> str:
    """``axiom_press__draft`` → ``press.draft`` (strip prefix, projector round-trip)."""
    core = tool_name[len(MCP_PREFIX):] if tool_name.startswith(MCP_PREFIX) else tool_name
    return surface_to_capability_name(core)


def is_mcp_exposed(spec: SkillSpec) -> bool:
    """True iff the capability opts into the MCP surface (AEOS §4.9.4).

    Undeclared ``surfaces`` means *not* exposed — exposure is bounded and
    explicit, never "every registered skill becomes an MCP tool".
    """
    return bool(spec.surfaces) and "mcp" in spec.surfaces


@dataclass(frozen=True)
class SkillToolContribution:
    """Registry-derived MCP tools + their dispatch handlers + identity-gate patterns."""

    tools: list[Tool]
    dispatch: dict[str, MCPHandler]
    allowed_principals: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _make_handler(
    registry: SkillRegistry, capability: str, ctx_factory: Callable[[], SkillContext]
) -> MCPHandler:
    async def handler(arguments: dict) -> dict:
        # An argument the spec never declared is refused, not forwarded
        # (ADR-157 Phase 0.2). Chat has refused these since ADR-072; MCP
        # forwarding them verbatim was the hole that let a caller pass
        # `dsn=` into a verb whose schema never mentioned one.
        from axiom.infra.capability_projection import undeclared_params
        from axiom.infra.skill_chain import split_chain_arg
        from axiom.infra.skill_declarations import APPROVAL_PARAM

        # The reserved chaining argument is bookkeeping, not a parameter: it is
        # separated before validation so it can never read as an undeclared one.
        arguments, chain = split_chain_arg(arguments)
        spec = registry.spec(capability)
        if spec is not None:
            declared_for_check = arguments
            if spec.effective_risk == "destructive":
                # The approval hash is part of every destructive tool's contract.
                declared_for_check = {k: v for k, v in arguments.items() if k != APPROVAL_PARAM}
            unknown = undeclared_params(spec, declared_for_check)
            if unknown:
                return {
                    "ok": False,
                    "value": None,
                    "errors": [
                        f"{capability} does not declare parameter(s): "
                        f"{', '.join(sorted(unknown))}"
                    ],
                    "actions_taken": [],
                }
        result = invoke_capability(
            registry,
            capability,
            arguments or {},
            ctx_factory(),
            surface=MCP_SURFACE,
            chain=chain,
        )
        payload = {
            "ok": result.ok,
            "value": result.value,
            "errors": list(result.errors),
            "actions_taken": list(result.actions_taken),
        }
        # Additive and versioned (ADR-185): present only when the call has
        # something to say about what came before or comes next.
        envelope = result.chain_envelope()
        if envelope is not None:
            payload["chain"] = envelope
        # No response outruns the agent-class byte budget (ADR-157 Phase 1.5):
        # an MCP consumer is a context window, and 327 KB of indented JSON is
        # how a harness refused a 459,096-character answer on 2026-10-02.
        from axiom.infra import serving_guard as sg

        budget = sg.load_policy().budget(
            cost_class=(spec.cost_class if spec is not None else None) or "lookup",
            caller_class="agent",
        )
        bounded, note = sg.bound_bytes(payload, max_bytes=budget.max_bytes)
        if note is not None:
            bounded.setdefault("notes", []).append(note)
        return bounded

    return handler


#: The single ``_meta`` key every Axiom chaining hint rides under (AEOS 0.2
#: §4.11.4). Reverse-DNS prefix, per the MCP rule for ``_meta`` keys, so a
#: client that does not know it ignores it and no other vendor's key can collide.
META_KEY = "ai.axiom/chain"


def tool_annotations(spec: SkillSpec) -> ToolAnnotations:
    """Project ``risk`` and ``idempotent`` onto the MCP tool annotation hints.

    Wire names (``mcp.types.ToolAnnotations`` aliases): ``readOnlyHint``,
    ``destructiveHint``, ``idempotentHint``.

    * ``risk=read`` -> readOnly true, destructive false.
    * ``risk=reversible-write`` -> readOnly false, destructive false.
    * ``risk=destructive`` -> readOnly false, destructive true.
    * ``risk`` undeclared -> readOnly from ``side_effects`` (as before) and
      ``destructiveHint`` left unset: "has not said" must not be projected as
      "harmless", and the protocol's own default for an unset destructive hint is
      the conservative one.
    * ``idempotent`` / ``open_world`` undeclared -> that hint left unset.
    * ``openWorldHint`` comes from ``open_world``; nothing is inferred.
    """
    risk = spec.risk
    read_only = is_read_only(spec) if risk is None else (risk == "read" and spec.side_effects is not True)
    destructive: bool | None = None if risk is None else risk == "destructive"
    return ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=spec.idempotent,
        open_world_hint=spec.open_world,
    )


def output_schema(spec: SkillSpec) -> dict | None:
    """The tool's ``outputSchema``, declared only when it ``produces`` refs (AEOS 0.2 §4.11.4).

    Produced refs are ordinary properties of ``value``, each tagged with the
    JSON Schema extension keyword ``x-aeos-ref`` (the ref kind), which a validator
    ignores and a chaining-aware client reads. The schema describes the whole
    structured result (``ok``, ``value``, ``errors``, ``chain``), because that is
    what ``structuredContent`` carries.
    """
    if not spec.produces:
        return None
    props = {
        r.key: {"x-aeos-ref": r.kind, "description": r.how_to_use} for r in spec.produces
    }
    return {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "value": {"type": ["object", "null"], "properties": props},
            "errors": {"type": "array", "items": {"type": "string"}},
            "actions_taken": {"type": "array"},
            "chain": {"type": "object"},
        },
        "required": ["ok"],
    }


def _link(spec: SkillSpec, step) -> dict:
    """One declared follow-up in OpenAPI Link Object vocabulary (AEOS 0.2 §4.11.5).

    ``operationId`` is the tool name (or ``command`` for a non-tool follow-up),
    ``parameters`` maps the target's input to a runtime expression over this
    tool's own structured result (``$response.body#/value/<ref>``, an RFC 6901
    JSON Pointer), ``description`` is the reason, and ``when`` is the AEOS
    condition OpenAPI Links do not have.
    """
    keys = {r.kind: r.key for r in (spec.produces or ())}
    link: dict = {}
    if step.is_skill:
        link["operationId"] = mcp_tool_name(step.target)
    else:
        link["command"] = step.target
    link["when"] = step.when
    link["description"] = step.why
    params = {
        name: f"$response.body#/value/{keys.get(kind, kind)}" for name, kind in dict(step.feeds).items()
    }
    if params:
        link["parameters"] = params
    return link


def chain_meta(spec: SkillSpec) -> dict | None:
    """The chaining declarations as a tool's ``_meta`` (AEOS 0.2 §4.11.4), or ``None``.

    A client that reads ``_meta`` learns what a tool needs, makes and offers next
    without a round trip. Data only: a precondition's ``check`` is code and never
    crosses the wire. Absent entirely when the spec declares nothing, so an
    undeclared tool lists byte-identically to before.
    """
    hints: dict = {}
    if spec.risk:
        hints["risk"] = spec.risk
    if spec.requires:
        hints["requires"] = [
            {"id": p.id, "fix": p.fix, **({"consumes": p.consumes} if p.consumes else {}),
             **({"param": p.param} if p.param else {})}
            for p in spec.requires
        ]
    if spec.produces:
        hints["produces"] = [{"kind": r.kind, "how_to_use": r.how_to_use} for r in spec.produces]
    if spec.next_steps:
        hints["next"] = [_link(spec, n) for n in spec.next_steps]
    if spec.recipes:
        hints["recipes"] = list(spec.recipes)
    return {META_KEY: {"v": 1, **hints}} if hints else None


def skill_tool_contribution(
    registry: SkillRegistry, *, ctx_factory: Callable[[], SkillContext]
) -> SkillToolContribution:
    """Build MCP tools + dispatch for every MCP-exposed registered capability.

    ``ctx_factory`` produces a fresh :class:`SkillContext` per call so each
    dispatch invokes the capability the same way the CLI does.
    """
    tools: list[Tool] = []
    dispatch: dict[str, MCPHandler] = {}
    allowed: dict[str, tuple[str, ...]] = {}
    for name, spec in sorted(registry.specs().items()):
        if not is_mcp_exposed(spec):
            continue
        tool_name = mcp_tool_name(name)
        schema = inputs_to_json_schema(spec.inputs)
        if spec.effective_risk == "destructive":
            schema["properties"].setdefault(
                "plan_hash",
                {
                    "type": "string",
                    "description": "hash of an approved plan; omit it to open a plan",
                },
            )
        # Identity gate (ADR-114 §1): a spec's declared principals, else the
        # owner-scoped default — a projected tool is never left unbound.
        allowed[tool_name] = tuple(spec.allowed_principals or DEFAULT_PATTERNS)
        tools.append(
            Tool(
                name=tool_name,
                description=spec.description or f"Invoke the {name} capability.",
                input_schema=schema,
                meta=chain_meta(spec),
                annotations=tool_annotations(spec),
                output_schema=output_schema(spec),
            )
        )
        dispatch[tool_name] = _make_handler(registry, name, ctx_factory)
    return SkillToolContribution(tools=tools, dispatch=dispatch, allowed_principals=allowed)


__all__ = [
    "META_KEY",
    "MCP_PREFIX",
    "MCPHandler",
    "SkillToolContribution",
    "is_mcp_exposed",
    "output_schema",
    "mcp_name_to_capability",
    "mcp_tool_name",
    "skill_tool_contribution",
    "tool_annotations",
]
