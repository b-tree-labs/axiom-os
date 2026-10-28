# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chat skills (ADR-056): the read forms of chat commands that mean something outside the chat loop.

Registered here so the composed MCP server projects them as ``axiom_chat__<verb>`` tools through the shared
projector (ADR-072/073). Which commands have a form here is declared on the command itself
(``Command.mcp``); ``test_chat_expose`` keeps this table and the registry in step. Only read forms are
registered: a destructive step goes through ``plan_apply``, never through a tool.

Every description says it is not the harness's own command of the same name (the MCP name-collision rules,
spec-builtin-mcp-server section 9.1), because a person types ``/tools`` into a harness that has its own.
"""

from __future__ import annotations

from dataclasses import replace

from axiom.infra.skill_declarations import NextStep, Precondition, Ref
from axiom.infra.skills import SkillRegistry, SkillSpec

from . import read

_MCP = ("mcp",)

#: skill -> (description, inputs). The description names what it is not.
_META: dict[str, tuple[str, dict[str, str]]] = {
    "chat.tools": (
        "List the tools the Axiom chat agent can call, each with its permission, whether it writes, and "
        "where it comes from. Not the harness's own /tools, which lists the harness's tools.",
        {"filter": "str", "session": "str"},
    ),
    "chat.stats": (
        "Per-turn latency, tokens, tool calls, cost and provider for a stored Axiom chat session, with "
        "the next steps the numbers earn. Not the harness's own /stats, which reports the harness session.",
        {"session": "str!"},
    ),
    "chat.where": (
        "Which model answers an Axiom chat, where it runs (local, cloud, remote node) and where retrieval "
        "comes from. Not the harness's own /where, which reports a harness location.",
        {"session": "str"},
    ),
    "chat.sources": (
        "What grounded the last reply of an Axiom chat and whether its citations hold. Not the harness's "
        "own /sources, which lists harness context sources.",
        {"session": "str"},
    ),
    "chat.skills": (
        "The platform's skills and whether the Axiom chat agent can call each now, and the setting that "
        "enables the rest. Not the harness's own /skills, which lists the harness's skill files.",
        {"filter": "str"},
    ),
    "chat.agents": (
        "The platform's declared agents: what each does, whether it runs and whether it is alive. Not the "
        "harness's own /agents, which manages the harness's subagents.",
        {"filter": "str"},
    ),
    "chat.mcp": (
        "The MCP surface Axiom provides: its servers and which harnesses have them registered. Not the "
        "harness's own /mcp, which manages the harness's MCP servers.",
        {},
    ),
    "chat.memory_search": (
        "Recall from the caller's Axiom memory through the serving gate; withheld content is counted and "
        "explained, never shown. Not the harness's own /memory, which edits the harness's memory file.",
        {"query": "str!", "k": "int", "session": "str"},
    ),
    "chat.memory_show": (
        "Open one Axiom memory by id or unique id prefix, through the serving gate. Not the harness's own "
        "/memory, which edits the harness's memory file.",
        {"id": "str!", "session": "str"},
    ),
    "chat.memory_recent": (
        "The caller's newest Axiom memories, through the serving gate. Not the harness's own /memory, "
        "which edits the harness's memory file.",
        {"n": "int", "session": "str"},
    ),
}

#: Skills whose command requires an administrator. The MCP identity gate (ADR-114) then admits only the
#: configured admin principals, and nobody when none is configured. Empty today: no admin command has a
#: read form here; the mechanism exists so one cannot be added without the gate.
ADMIN_ONLY: frozenset[str] = frozenset()
NO_ADMIN_PATTERN: tuple[str, ...] = ("@__no_admin_resolved__:__none__",)

#: Chaining declarations (ADR-185). Every chat skill is a read, so none owes a precondition; the two memory
#: reads that hand an id along say so, because a remembered claim is checked by opening what it came from.
_FRAGMENT_ID = Ref("fragment_id", "pass as id to chat.memory_show to open the memory and read where it came from")
_FRAGMENT_GIVEN = Precondition(
    "fragment-given",
    "the memory to open must be named by id",
    consumes="fragment_id",
    param="id",
    fix="find one with chat.memory_search (it returns fragment_id) or chat.memory_recent, and pass it as id",
)
_CHAINING: dict[str, dict] = {
    "chat.where": {
        "next_steps": (NextStep("chat.sources", "ok", "see what grounded the last reply"),),
        "recipes": ("session-handoff",),
    },
    "chat.memory_search": {
        "produces": (_FRAGMENT_ID,),
        "next_steps": (
            NextStep("chat.memory_show", "produced:fragment_id", "open the first result and read where it came from",
                     feeds={"id": "fragment_id"}),
        ),
        "recipes": ("memory-verify",),
    },
    "chat.memory_recent": {
        "produces": (_FRAGMENT_ID,),
        "next_steps": (
            NextStep("chat.memory_show", "produced:fragment_id", "open the newest one in full",
                     feeds={"id": "fragment_id"}),
        ),
    },
    "chat.memory_show": {"requires": (_FRAGMENT_GIVEN,), "recipes": ("memory-verify",)},
    "chat.tools": {"next_steps": (NextStep("chat.skills", "ok", "see which platform skills the chat agent can call"),)},
    "chat.skills": {"next_steps": (NextStep("chat.tools", "ok", "see the tools the chat agent can call now"),)},
    "chat.sources": {"next_steps": (NextStep("chat.where", "ok", "see where the model and retrieval run"),)},
}

SPECS: tuple[SkillSpec, ...] = tuple(
    SkillSpec(
        name=name,
        fn=fn,
        description=_META[name][0],
        inputs=_META[name][1],
        surfaces=_MCP,
        side_effects=False,
        idempotent=True,
        cost_class="lookup",
        risk="read",
        **_CHAINING.get(name, {}),
    )
    for name, fn in read.FUNCTIONS.items()
)


def admin_principals() -> tuple[str, ...]:
    """The principals the MCP gate admits for an admin-only skill; a pattern that matches no one when the
    gate resolves none (never "everyone")."""
    from ..roles import _configured_admins

    configured = tuple(p for p in _configured_admins() if p)
    return configured or NO_ADMIN_PATTERN


def gated(spec: SkillSpec) -> SkillSpec:
    return replace(spec, allowed_principals=admin_principals()) if spec.name in ADMIN_ONLY else spec


def bind(registry: SkillRegistry) -> None:
    for spec in SPECS:
        if not registry.has(spec.name):
            registry.register_skill(gated(spec), mutating=False)


def bind_default() -> SkillRegistry:
    registry = SkillRegistry()
    bind(registry)
    return registry


__all__ = ["ADMIN_ONLY", "SPECS", "bind", "bind_default", "gated"]
