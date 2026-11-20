# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""MVP-02-A5 (DC-16): every tool chat can call is an MCP tool with the same result.

A capability reaches chat through the shared projector (``capability_projection``)
when it declares ``agent_tool``; it reaches MCP when it declares ``mcp``. A
capability declaring the first and not the second is a chat-only tool, which is
the failure this row exists to catch.

Chat also still carries tools that predate the projector (its built-ins, its
``tools_ext`` modules, and manifest tools whose MCP name differs). Those are
named in :data:`KNOWN_CHAT_ONLY`: a ratchet that only shrinks. A new chat-only
tool fails here; moving one onto a skill deletes its line.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import pkgutil

import pytest

from axiom.infra.capability_projection import capability_to_surface_name, exposed_on
from axiom.infra.skills import SkillContext, SkillRegistry

#: Chat tools with no MCP tool of the same capability, as found on 2026-10-30. Each is
#: debt: it should become a skill declaring both ``agent_tool`` and ``mcp``.
KNOWN_CHAT_ONLY = frozenset(
    {
        # chat built-ins (tools/__init__.py)
        "discover_verbs",
        "doc_check_links",
        "doc_diff",
        "doc_generate",
        "doc_publish",
        "list_providers",
        "plane_status",
        "query_docs",
        "signal_ingest",
        "signal_status",
        "write_file",
        "write_inbox_note",
        # chat tools_ext modules
        "agent_actions",
        "agent_stop_task",
        "agent_task_status",
        "delegate_to_agent",
        "email_draft",
        "email_list",
        "email_preview",
        "email_send",
        "list_files",
        "read_file",
        "review_complete",
        "review_decide",
        "review_get_item",
        "review_progress",
        "review_start",
        "search_docs",
        "verify_prediction",
        # manifest tools whose MCP tool carries a different name
        "analytics",
        "authz_decide",
        "hygiene_status",
        "notifications_list",
        "node_describe",
        "pr_lander_report",
        "receipts_receipt",
        "signals_status",
        "vault_get_capability",
    }
)


def _all_skills() -> SkillRegistry:
    import axiom.extensions.builtins as builtins

    registry = SkillRegistry()
    for mod in pkgutil.iter_modules(builtins.__path__):
        try:
            pkg = importlib.import_module(f"axiom.extensions.builtins.{mod.name}.skills")
        except Exception:  # noqa: BLE001 - an extension without skills offers none
            continue
        binder = getattr(pkg, "bind", None) or getattr(pkg, "register", None)
        if callable(binder):
            try:
                binder(registry)
            except Exception:  # noqa: BLE001
                continue
    return registry


@pytest.fixture(scope="module")
def registry() -> SkillRegistry:
    return _all_skills()


def test_mvp_02_a5_every_capability_chat_offers_is_an_mcp_tool(registry: SkillRegistry) -> None:
    from axiom.extensions.builtins.chat.skill_tools import skills_to_tool_definitions
    from axiom.extensions.builtins.mcp.skill_tools import mcp_tool_name, skill_tool_contribution

    chat = {t.name for t in skills_to_tool_definitions(registry)}
    assert len(chat) > 50, "the projection found the capabilities"
    mcp = {t.name for t in skill_tool_contribution(registry, ctx_factory=lambda: None).tools}
    missing = sorted(n for n in chat if "axiom_" + n not in mcp)
    assert not missing, f"chat-only capability tools (declare surfaces 'mcp' too): {missing}"
    chat_only_specs = [
        n
        for n, s in registry.specs().items()
        if exposed_on(s, "agent_tool") and not exposed_on(s, "mcp")
    ]
    assert not chat_only_specs
    assert mcp_tool_name("charts.suggest") == "axiom_" + capability_to_surface_name(
        "charts.suggest"
    )


def test_mvp_02_a5_chat_and_mcp_return_the_same_payload(
    registry: SkillRegistry, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from axiom.extensions.builtins.chat import tools as chat_tools
    from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution

    params = {
        "result": {
            "series": [
                {
                    "name": "inlet",
                    "unit": "degC",
                    "x": [f"2026-10-30T00:0{i}:00Z" for i in range(6)],
                    "y": [20.0, 21.5, 21.0, 22.0, 23.5, 23.0],
                }
            ]
        },
        "question": "how did it behave",
    }

    monkeypatch.setattr(chat_tools, "_skill_registry", lambda: registry)
    monkeypatch.setattr(
        chat_tools,
        "_skill_context",
        lambda reg: SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t")),
    )
    via_chat = chat_tools._execute_skill_tool("charts__suggest", dict(params))

    contrib = skill_tool_contribution(
        registry,
        ctx_factory=lambda: SkillContext(
            registry=registry, state_dir=tmp_path, logger=logging.getLogger("t")
        ),
    )
    via_mcp = asyncio.run(contrib.dispatch["axiom_charts__suggest"](dict(params)))

    assert via_chat["ok"] is True and via_mcp["ok"] is True, (
        via_chat.get("errors"),
        via_mcp.get("errors"),
    )
    assert via_chat["value"] == via_mcp["value"]
    assert via_chat["errors"] == via_mcp["errors"]


def test_mvp_02_a5_no_new_chat_only_tool(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The legacy (non-skill) chat tools all reach MCP, or are named debt above."""
    from axiom.extensions.builtins.chat import tools as chat_tools
    from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry

    monkeypatch.setenv("HOME", str(tmp_path))  # no user extensions: the in-tree surface only
    legacy = dict(chat_tools.BUILTIN_TOOLS)
    legacy.update(chat_tools._scan_extensions())
    legacy.update(chat_tools._scan_manifest_tools(taken=set(legacy)))
    mcp = {t.name for t in AggregationRegistry.from_node().build().tools}

    def reachable(name: str) -> bool:
        return f"axiom_{name}" in mcp or any(m.endswith(f"__{name}") for m in mcp)

    chat_only = {n for n in legacy if not reachable(n)}
    new = sorted(chat_only - KNOWN_CHAT_ONLY)
    assert not new, (
        f"new chat-only tools; project them from a skill with surfaces agent_tool+mcp: {new}"
    )
    fixed = sorted((KNOWN_CHAT_ONLY & set(legacy)) - chat_only)
    assert not fixed, f"these reach MCP now; delete their KNOWN_CHAT_ONLY lines: {fixed}"
