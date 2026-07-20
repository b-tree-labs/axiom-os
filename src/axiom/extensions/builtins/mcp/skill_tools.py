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
- **Side-effects:** the capability's declaration → MCP ``readOnlyHint`` /
  ``idempotentHint`` annotations.
- **Dispatch:** ``SkillRegistry.invoke(capability, args, ctx)`` — the same
  entry point the CLI verb uses.
- **Exposure:** a capability is an MCP tool iff it opts in via
  ``surfaces=[…"mcp"]`` (AEOS §4.9.4) — the bounded-exposure guard.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from mcp.types import Tool, ToolAnnotations

from axiom.infra.capability_projection import (
    capability_to_surface_name,
    inputs_to_json_schema,
    is_read_only,
    surface_to_capability_name,
)
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
    """Registry-derived MCP tools + their dispatch handlers."""

    tools: list[Tool]
    dispatch: dict[str, MCPHandler]


def _make_handler(
    registry: SkillRegistry, capability: str, ctx_factory: Callable[[], SkillContext]
) -> MCPHandler:
    async def handler(arguments: dict) -> dict:
        result = registry.invoke(capability, arguments or {}, ctx_factory())
        return {
            "ok": result.ok,
            "value": result.value,
            "errors": list(result.errors),
            "actions_taken": list(result.actions_taken),
        }

    return handler


def skill_tool_contribution(
    registry: SkillRegistry, *, ctx_factory: Callable[[], SkillContext]
) -> SkillToolContribution:
    """Build MCP tools + dispatch for every MCP-exposed registered capability.

    ``ctx_factory`` produces a fresh :class:`SkillContext` per call so each
    dispatch invokes the capability the same way the CLI does.
    """
    tools: list[Tool] = []
    dispatch: dict[str, MCPHandler] = {}
    for name, spec in sorted(registry.specs().items()):
        if not is_mcp_exposed(spec):
            continue
        tool_name = mcp_tool_name(name)
        tools.append(
            Tool(
                name=tool_name,
                description=spec.description or f"Invoke the {name} capability.",
                inputSchema=inputs_to_json_schema(spec.inputs),
                annotations=ToolAnnotations(
                    readOnlyHint=is_read_only(spec),
                    idempotentHint=spec.idempotent,
                ),
            )
        )
        dispatch[tool_name] = _make_handler(registry, name, ctx_factory)
    return SkillToolContribution(tools=tools, dispatch=dispatch)


__all__ = [
    "MCP_PREFIX",
    "MCPHandler",
    "SkillToolContribution",
    "is_mcp_exposed",
    "mcp_name_to_capability",
    "mcp_tool_name",
    "skill_tool_contribution",
]
