# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Argument completers for the built-in slash commands.

Each is ``complete(agent, args_so_far) -> [(value, description), ...]``: the hook ``Command.complete``
declares and ``completion.suggest`` calls for the word being typed. ``args_so_far`` holds the finished
words after the command. They are cheap, read-only, and return ``[]`` when they cannot say (no agent, no
sessions): completion must never raise into the prompt.

``wire_completers`` attaches them to the registered commands. A command that already declares its own
``complete`` (another module's) is left alone.
"""

from __future__ import annotations

from typing import Any

_MODES = [("allow", "Run without asking"), ("ask", "Ask each time"), ("deny", "Never run")]
_ON_OFF = [("on", "Turn on"), ("off", "Turn off"), ("status", "Show the current state")]


def complete_model(agent: Any, args: list[str]) -> list[tuple[str, str]]:
    """Provider names, each labelled with where it runs."""
    if args or agent is None:
        return []
    from axiom.llm.placement import placement_of

    out = []
    for p in agent.gateway.providers:
        out.append((p.name, f"{p.model}  {placement_of(p).label()}  [{p.routing_tier}]"))
    return out


def _recent_sessions(agent: Any = None, limit: int = 15) -> list[tuple[str, str]]:
    """The viewer's own recent sessions (titles are content: another principal's are not offered)."""
    from axiom.infra.orchestrator.session import SessionStore

    store = SessionStore()
    principal = getattr(getattr(agent, "session", None), "principal_id", "") or ""
    out = []
    for sid in store.list_sessions(principal=principal)[:limit]:
        meta = store.load_meta(sid) or {}
        out.append((sid, meta.get("title") or "(untitled)"))
    return out


def complete_resume(agent: Any, args: list[str]) -> list[tuple[str, str]]:
    return [] if args else _recent_sessions(agent)


def complete_sessions(agent: Any, args: list[str]) -> list[tuple[str, str]]:
    # `/sessions archive <id>` takes a session; the subcommand words themselves come from the declaration.
    return _recent_sessions(agent) if args == ["archive"] else []


def _tool_names() -> list[tuple[str, str]]:
    from .tools import get_all_tools

    return [
        (name, (t.description or "").strip().split("\n")[0][:80])
        for name, t in sorted(get_all_tools().items())
    ]


def complete_permissions(agent: Any, args: list[str]) -> list[tuple[str, str]]:
    if not args:
        return [("set", "Set a tool's mode"), ("reset", "Clear one tool's override, or all")]
    if args[0] == "set":
        if len(args) == 1:
            return _tool_names()
        if len(args) == 2:
            return list(_MODES)
    if args[0] == "reset" and len(args) == 1:
        return _tool_names()
    return []


def complete_where(agent: Any, args: list[str]) -> list[tuple[str, str]]:
    return [] if args else [("--json", "Machine-readable report")]


def complete_on_off(agent: Any, args: list[str]) -> list[tuple[str, str]]:
    return [] if args else list(_ON_OFF)


_BUILTIN = {
    "/model": complete_model,
    "/resume": complete_resume,
    "/sessions": complete_sessions,
    "/permissions": complete_permissions,
    "/where": complete_where,
    "/rag": complete_on_off,
    "/trace": complete_on_off,
}


def wire_completers(registry: Any) -> None:
    """Attach the built-in completers to any registered command that declares none. Idempotent."""
    for name, fn in _BUILTIN.items():
        cmd = registry.get(name)
        if cmd is not None and cmd.complete is None:
            registry.set_completer(name, fn)
