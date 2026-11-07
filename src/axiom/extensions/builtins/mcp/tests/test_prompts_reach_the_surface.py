# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A prompt an extension declares must reach the harnesses we publish to.

AEOS §4.8 defines the `prompt` capability, and names the point of it: the Axi
MCP server publishes templated prompts to external harnesses — Claude Code,
Cursor, Codex, OpenCode — as the cross-harness gravity mechanism. The manifest
schema parses `[[extension.mcp.prompt]]` into `MCPPromptDecl`, the server serves
`surface.prompts`, and the HTTP mount answers `prompts/list`.

Every link existed except one. The aggregator read `cfg.prompts` — only to
decide whether an extension was worth including — and then never merged them.
`merged_prompts` was initialised empty and extended by nothing.

Verified against a live server before this change: capabilities advertised
`prompts`, and `prompts/list` returned zero. A declared capability with no path
to the wire is the same shape as the notifications table that had no writers.
"""

from __future__ import annotations

from axiom.extensions.builtins.mcp.manifest_schema import MCPPromptDecl


def test_a_declared_prompt_is_carried_to_the_surface():
    from axiom.extensions.builtins.mcp.aggregation import _prompts_for_extension

    decls = (MCPPromptDecl(name="telemetry.monitor-authoring",
                           description="How to write a monitor",
                           entry="mod:fn"),)

    prompts = _prompts_for_extension("triga_telemetry", decls)

    assert [p.name for p in prompts] == ["telemetry.monitor-authoring"]


def test_the_description_survives_so_a_harness_can_show_it():
    from axiom.extensions.builtins.mcp.aggregation import _prompts_for_extension

    decls = (MCPPromptDecl(name="a.b", description="Pick a threshold from history",
                           entry="mod:fn"),)

    assert _prompts_for_extension("x", decls)[0].description == (
        "Pick a threshold from history"
    )


def test_declared_arguments_are_published():
    """A harness needs the arguments to fill the template in."""
    from axiom.extensions.builtins.mcp.aggregation import _prompts_for_extension

    decls = (MCPPromptDecl(name="a.b", description="d", entry="m:f",
                           arguments=("metric", "window")),)

    names = [a.name for a in (_prompts_for_extension("x", decls)[0].arguments or [])]
    assert names == ["metric", "window"]


def test_an_extension_declaring_none_contributes_none():
    """Negative control: this must not invent prompts for every extension."""
    from axiom.extensions.builtins.mcp.aggregation import _prompts_for_extension

    assert _prompts_for_extension("x", ()) == []


def test_a_malformed_declaration_is_skipped_not_fatal():
    """One bad manifest must not take the whole MCP surface down."""
    from axiom.extensions.builtins.mcp.aggregation import _prompts_for_extension

    decls = (MCPPromptDecl(name="", description="d", entry="m:f"),)

    assert _prompts_for_extension("x", decls) == []
