# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Published prompts have to be reachable by harnesses that only speak tools.

Measured against three installed harnesses with a probe MCP server that
advertised both tools and prompts, logging every method each client called:

  Codex 0.150.0-alpha.8   initialize, notifications/initialized, tools/list
  OpenCode 1.17.8         initialize, notifications/initialized, tools/list
  Hermes 0.20.4           the same three; prompts only via an on-demand handler

Two of three never ask for prompts at all. So a server can publish a perfectly
conformant prompt and it reaches nobody — which is what our
`telemetry.monitor-authoring` prompt did the moment it shipped.

OpenClaw solved this by not waiting for clients to adopt `prompts/list`: it
projects prompts into the tool surface as generated utility tools. Tools are the
one surface every harness already speaks. This is that projection.

The projection is additive — `prompts/list` still works for clients that use
it, and this gives the same content a second door.
"""

from __future__ import annotations

from axiom.extensions.builtins.mcp.aggregation import build_prompt_access_tools
from axiom.extensions.builtins.mcp.manifest_schema import MCPPromptDecl


def _decl(name="ext.guidance", entry="mod:fn", args=("topic",)):
    return MCPPromptDecl(name=name, description="How to do the thing",
                         entry=entry, arguments=args)


def test_no_prompts_means_no_projection():
    """Negative control: a server with no prompts must not grow phantom tools."""
    tools, dispatch = build_prompt_access_tools({})

    assert tools == []
    assert dispatch == {}


def test_prompts_produce_list_and_get_tools():
    tools, dispatch = build_prompt_access_tools({"ext.guidance": _decl()})

    names = {t.name for t in tools}
    assert names == {"axiom_prompts__list", "axiom_prompts__get"}
    assert set(dispatch) == names


def test_the_list_tool_names_every_published_prompt():
    tools, dispatch = build_prompt_access_tools({
        "a.one": _decl("a.one"), "b.two": _decl("b.two"),
    })

    import asyncio
    result = asyncio.run(dispatch["axiom_prompts__list"]({}))

    listed = {p["name"] for p in result["prompts"]}
    assert listed == {"a.one", "b.two"}


def test_the_list_tool_carries_descriptions_and_arguments():
    """A harness choosing between prompts needs more than a name."""
    import asyncio
    _, dispatch = build_prompt_access_tools({"a.one": _decl("a.one")})

    entry = asyncio.run(dispatch["axiom_prompts__list"]({}))["prompts"][0]

    assert entry["description"]
    assert entry["arguments"] == ["topic"]


def test_get_resolves_the_declared_entry_and_returns_content():
    import asyncio
    decl = _decl(entry="axiom.extensions.builtins.mcp.tests.test_prompts_as_tools:_sample")
    _, dispatch = build_prompt_access_tools({"ext.guidance": decl})

    out = asyncio.run(dispatch["axiom_prompts__get"]({"name": "ext.guidance"}))

    assert out["ok"] is True
    assert "SAMPLE CONTENT" in out["content"]


def test_get_passes_arguments_through():
    import asyncio
    decl = _decl(entry="axiom.extensions.builtins.mcp.tests.test_prompts_as_tools:_sample")
    _, dispatch = build_prompt_access_tools({"ext.guidance": decl})

    out = asyncio.run(
        dispatch["axiom_prompts__get"]({"name": "ext.guidance", "topic": "rods"}))

    assert "rods" in out["content"]


def test_an_unknown_prompt_is_an_honest_error_not_an_empty_string():
    """An empty string reads as "this prompt says nothing"; it must not."""
    import asyncio
    _, dispatch = build_prompt_access_tools({"ext.guidance": _decl()})

    out = asyncio.run(dispatch["axiom_prompts__get"]({"name": "nope"}))

    assert out["ok"] is False
    assert "nope" in out["error"]
    assert "ext.guidance" in out["error"], "it should say what does exist"


def test_a_broken_entry_reports_rather_than_crashing_the_server():
    """One bad prompt handler must not take the MCP surface down."""
    import asyncio
    decl = _decl(entry="no.such.module:missing")
    _, dispatch = build_prompt_access_tools({"ext.guidance": decl})

    out = asyncio.run(dispatch["axiom_prompts__get"]({"name": "ext.guidance"}))

    assert out["ok"] is False
    assert "ext.guidance" in out["error"]


def test_get_requires_a_name():
    import asyncio
    _, dispatch = build_prompt_access_tools({"ext.guidance": _decl()})

    out = asyncio.run(dispatch["axiom_prompts__get"]({}))

    assert out["ok"] is False


def _sample(args):
    """Test prompt handler."""
    topic = (args or {}).get("topic", "")
    return {"ok": True, "content": f"SAMPLE CONTENT {topic}".strip()}
