# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Project registered capabilities as agent-facing LLM tools (AEOS §4.9).

An orchestrating agent (AXI, or a domain prime like Neut) calls sub-agents'
capabilities. For its LLM to do that, each `SkillRegistry` capability must
appear as a function-calling tool. This module is the **agent_tool**
projection of AEOS §4.9 — it is *not* a converter of its own: name-mangling,
schema derivation, and the READ/WRITE→approval decision all come from the
shared projector `axiom.infra.capability_projection` (ADR-072), the same
machinery the CLI and MCP surfaces use. This module only assembles the
chat-loop's :class:`ToolDef` from those projected parts.

Scoping (the tool-explosion guard, §4.9.4): `namespace` filters to a single
agent's capabilities. An agent's tool list is a *bounded* projection of the
capabilities it is authorized to call — never "all skills." (Per-capability
`exposed_to_agents` enforcement arrives with the manifest fields; until then
callers scope via `namespace`.) The chat loop itself scopes through the
``chat.tool_namespaces`` setting, read in :func:`tools.get_all_tools`.

Approval is read from the capability's declared `side_effects`
(`capability_projection.approval_category`) — no per-surface "default WRITE".
"""

from __future__ import annotations

import logging

from axiom.extensions.builtins.chat.tools import ToolDef
from axiom.infra.capability_projection import (
    approval_category,
    capability_to_surface_name,
    exposed_on,
    inputs_to_json_schema,
    surface_to_capability_name,
)
from axiom.infra.skills import SkillRegistry

logger = logging.getLogger(__name__)

#: The surface a capability must declare to reach a conversation's tool list.
AGENT_SURFACE = "agent_tool"

# Re-exported for the dispatcher's tool-call → skill-name recovery. These are
# the projector's canonical round-trip, not a local convention.
skill_to_tool_name = capability_to_surface_name
tool_to_skill_name = surface_to_capability_name


def skills_to_tool_definitions(
    registry: SkillRegistry,
    *,
    namespace: str | None = None,
) -> list[ToolDef]:
    """Project a (scoped) set of capabilities into chat-loop :class:`ToolDef`s.

    ``namespace`` filters to one agent's capabilities (e.g. ``"press"``).
    Names, schema, and approval category are derived by the shared projector;
    READ/WRITE comes from each capability's declared ``side_effects``.

    A capability reaches this list only if it declared ``agent_tool`` among
    its ``surfaces``. That declaration existed and was never consulted, so
    naming a namespace handed the model everything in it: ``data`` offered
    all 39 of its capabilities when 12 had declared themselves agent-facing
    and the other 27 had said nothing. Exposing what has not opted in is
    how a tool list becomes too long to be any use, and how a verb nobody
    meant to offer becomes callable from a conversation.
    """
    tools: list[ToolDef] = []
    withheld: list[str] = []
    for name, spec in sorted(registry.specs().items()):
        if namespace is not None and not name.startswith(f"{namespace}."):
            continue
        if not exposed_on(spec, AGENT_SURFACE):
            withheld.append(name)
            continue
        tools.append(
            ToolDef(
                name=capability_to_surface_name(name),
                description=spec.description or f"Invoke the {name} capability.",
                category=approval_category(spec),
                parameters=inputs_to_json_schema(getattr(spec, "inputs", {})),
            )
        )
    if withheld:
        # An empty tool list after configuring a namespace is otherwise a
        # mystery. Say which capabilities stayed out and why, so the answer
        # is "they have not declared agent_tool" rather than a shrug.
        logger.debug(
            "agent_tool projection withheld %d capability(ies) that did not declare "
            "the surface: %s",
            len(withheld),
            ", ".join(withheld),
        )
    return tools


__all__ = [
    "skill_to_tool_name",
    "skills_to_tool_definitions",
    "tool_to_skill_name",
]
