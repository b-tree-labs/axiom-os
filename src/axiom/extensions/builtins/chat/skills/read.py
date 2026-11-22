# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Pure skill functions behind the read forms of the chat commands (ADR-056).

Each function takes ``(params, ctx)`` and returns a ``SkillResult`` whose ``value`` is JSON-serializable.
The slash command, ``axi chat --<verb> --json`` and the ``axiom_chat__<verb>`` MCP tool all end in the same
``*_data`` function in the command's own module, so the three cannot disagree. Nothing here reads or returns
a secret: the data functions return names, states and ids, and memory reads go through the serving gate.

A function that needs a conversation builds a real ``ChatAgent`` over a stored session (``session`` param) or
a fresh one. ``agent_for`` is the one place that happens.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def _fail(message: str) -> SkillResult:
    return SkillResult(ok=False, value=None, errors=[message])


def agent_for(params: dict[str, Any]):
    """A real chat agent over the named stored session, or over a fresh one. ``(agent, error)``."""
    from axiom.infra.bus import EventBus
    from axiom.infra.gateway import Gateway
    from axiom.infra.orchestrator.session import SessionStore

    from ..agent import ChatAgent

    session = None
    sid = (params.get("session") or "").strip()
    if sid:
        session = SessionStore().load(sid)
        if session is None:
            return None, f"no stored session {sid!r}"
    # The same model resolution chat answers with, so /where and --where report the model --ask
    # would use (the bound node's, when bound), not a bare gateway's guess.
    from ..bound_model import choose_default_model

    gateway = Gateway()
    choose_default_model(gateway)
    return ChatAgent(gateway=gateway, bus=EventBus(), session=session), None


def _with_agent(params, build):
    agent, err = agent_for(params)
    if err:
        return _fail(err)
    return SkillResult(ok=True, value=build(agent))


def tools(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from ..commands_ext.tools_cmd import tools_data

    return _with_agent(params, lambda a: tools_data(a, str(params.get("filter") or "")))


def stats(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from ..commands_ext.stats import stats_data

    return _with_agent(params, lambda a: stats_data(a, surface="mcp"))


def where(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from ..commands_ext.where_cmd import where_data

    return _with_agent(params, where_data)


def sources(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from ..commands_ext.sources import sources_data

    return _with_agent(params, sources_data)


def skills(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from ..commands_ext.skills_cmd import skills_data

    return SkillResult(ok=True, value=skills_data(str(params.get("filter") or "")))


def agents(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from ..commands_ext.agents_cmd import agents_data

    return SkillResult(ok=True, value=agents_data(str(params.get("filter") or "")))


def mcp(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from ..commands_ext.mcp_cmd import mcp_data

    return SkillResult(ok=True, value=mcp_data())


def memory_search(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from ..commands_ext.memory import memory_search_data

    return _with_memory(params, lambda a: memory_search_data(a, str(params.get("query") or ""), params.get("k") or 5))


def memory_show(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from ..commands_ext.memory import memory_show_data

    return _with_memory(params, lambda a: memory_show_data(a, str(params.get("id") or "")))


def memory_recent(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from ..commands_ext.memory import memory_recent_data

    return _with_memory(params, lambda a: memory_recent_data(a, params.get("n") or 5))


def _with_memory(params, call) -> SkillResult:
    agent, err = agent_for(params)
    if err:
        return _fail(err)
    data = call(agent)
    if isinstance(data, dict) and data.get("error"):
        return _fail(str(data["error"]))
    if isinstance(data, dict):
        # A scalar reference a later step can use (ADR-185): the first memory served. The ids are ids,
        # never content, and a withheld item is not in `items`, so nothing the gate held back leaks here.
        items = data.get("items")
        first = items[0].get("id") if isinstance(items, list) and items and isinstance(items[0], dict) else None
        if first and "fragment_id" not in data:
            data = {**data, "fragment_id": first}
    return SkillResult(ok=True, value=data)


#: skill name -> function. The names are the ``McpForm.skill`` values the registry declares.
FUNCTIONS = {
    "chat.tools": tools,
    "chat.stats": stats,
    "chat.where": where,
    "chat.sources": sources,
    "chat.skills": skills,
    "chat.agents": agents,
    "chat.mcp": mcp,
    "chat.memory_search": memory_search,
    "chat.memory_show": memory_show,
    "chat.memory_recent": memory_recent,
}
