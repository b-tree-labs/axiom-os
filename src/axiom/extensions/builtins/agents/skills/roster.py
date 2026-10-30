# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Who can be asked, and what each one is for."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def owned_tools(record: Any) -> list[str]:
    """The MCP tool names an agent owns: its namespaces' MCP-exposed skills.

    Binds only the agent's own extensions into a scratch registry, so listing
    the roster never imports the whole node. A non-builtin extension that
    cannot be imported this way contributes nothing rather than failing.
    """
    import importlib

    from axiom.extensions.builtins.mcp.skill_tools import is_mcp_exposed, mcp_tool_name
    from axiom.infra.skills import SkillRegistry

    reg = SkillRegistry()
    for ext in record.extensions:
        try:
            importlib.import_module(f"axiom.extensions.builtins.{ext}.skills").bind(reg)
        except Exception:  # noqa: BLE001 — a roster lists what it can see
            continue
    spaces = set(record.namespaces)
    return sorted(
        mcp_tool_name(name)
        for name, spec in reg.specs().items()
        if name.split(".", 1)[0] in spaces and is_mcp_exposed(spec)
    )


def roster(params: dict[str, Any] | None = None, ctx: SkillContext | None = None, **_: Any) -> SkillResult:
    """List every agent ``agents.ask`` can address, with what it is for.

    ``agents`` stays the bare handle list older callers read; ``records``
    carries each agent's role, the domain it owns, its extensions, its tools
    and example requests — what a harness needs to route "ask KEEP" without
    being told who KEEP is.

    ``SkillRegistry.invoke`` calls every skill as ``fn(params, ctx)``. A roster
    that only accepted keywords never ran from MCP.
    """
    del params, ctx
    from axiom.agents.roster import load_roster

    records = load_roster()
    return SkillResult(
        ok=True,
        value={
            "agents": [r.handle for r in records],
            "records": [r.to_dict(tools=owned_tools(r)) for r in records],
        },
    )


__all__ = ["owned_tools", "roster"]
