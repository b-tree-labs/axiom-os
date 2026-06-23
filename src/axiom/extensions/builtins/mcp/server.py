# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Built-in root MCP server — stdio transport.

Spec: ``docs/specs/spec-builtin-mcp-server.md`` §5.

Entry point: ``python -m axiom.extensions.builtins.mcp.server``.

The server loads the cached/fresh aggregated surface once at start, then
serves MCP requests over stdio. Per spec §5.3, surface mutations during
the lifetime of a running subprocess do not affect it; on next spawn the
freshly cached surface is loaded.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Prompt, Resource, TextContent, Tool

from axiom.extensions.builtins.mcp.aggregation import (
    AggregationRegistry,
    MCPSurface,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------------

# MCP `serverInfo.instructions` — shown to every connecting client at the
# protocol handshake. Tells peer harnesses (Claude Code, Cursor, Cline,
# Goose, ChatGPT Desktop, Claude.ai connectors, Codex, …) what they're
# connected to + the load-bearing conventions of the platform. Per the
# `prd-builtin-mcp-server.md` §5.2 addition (ADR-052) and tracked in
# axiom-os#268 Item 1.
SERVER_INSTRUCTIONS = (
    "Axiom is a domain-agnostic agentic platform with conformant extensions. "
    "When working on an extension that needs persistence, use "
    '`axiom.infra.db.session_for("<ext>")` — never construct your own engine, '
    "never write to `public`, never hardcode `schema=` on tables (per ADR-052). "
    "Cross-extension reads ride the data platform (ADR-049), not OLTP joins. "
    "Within-extension tenancy is a three-option menu: single-tenant (default), "
    "row-level `tenant_id`, or schema-per-tenant (ADR-052 §D4). "
    "Use the `axiom_db__*` tools, when available, to introspect installed "
    "extension schemas + migration state."
)


def build_server(surface: MCPSurface) -> Server:
    """Wire an ``mcp.server.Server`` against the given surface.

    The handlers are closures over ``surface`` so a fresh build for each
    subprocess is straightforward.

    The server's ``instructions`` payload is set to ``SERVER_INSTRUCTIONS`` so
    every connecting client sees the load-bearing platform conventions at the
    protocol handshake.
    """
    server: Server = Server("axiom-root", instructions=SERVER_INSTRUCTIONS)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:  # pragma: no cover — exercised in integration
        return list(surface.tools)

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[TextContent]:  # pragma: no cover — exercised in integration
        return await dispatch_call(surface, name, arguments or {})

    @server.list_resources()
    async def _list_resources() -> list[Resource]:  # pragma: no cover
        return list(surface.resources)

    @server.list_prompts()
    async def _list_prompts() -> list[Prompt]:  # pragma: no cover
        return list(surface.prompts)

    return server


# ---------------------------------------------------------------------------
# Dispatch — separated so it can be exercised in unit tests without spawning
# the SDK's request loop
# ---------------------------------------------------------------------------


# Routing wire-in: classification-aware routing lives in
# `axiom.extensions.builtins.mcp.routing` (shipped in PR #139). Per the
# wire-in pattern documented there, the server activation site (where a
# QueryRouter + PeerRegistry are constructed from settings) wraps the
# dispatch_call below:
#
#   from axiom.extensions.builtins.mcp.routing import wrap_dispatcher
#   dispatch_call = wrap_dispatcher(dispatch_call, router=..., peers=...)
#
# Activating this needs a settings-driven router instance and is staged
# for the server CLI bring-up, not the surface-aggregation layer here.


async def dispatch_call(
    surface: MCPSurface, name: str, arguments: dict[str, Any]
) -> list[TextContent]:
    """Run a tool handler against the given surface; return MCP wire content.

    Wraps every exception in a structured JSON error so an MCP client
    never sees a hard crash on a single bad call.
    """
    handler = surface.dispatch.get(name)
    if handler is None:
        payload = {"error": f"unknown tool: {name}"}
        return [TextContent(type="text", text=json.dumps(payload))]
    try:
        result = await handler(arguments)
    except Exception as exc:  # noqa: BLE001 — translate every error
        payload = {"error": f"{type(exc).__name__}: {exc}"}
        return [TextContent(type="text", text=json.dumps(payload))]
    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


# ---------------------------------------------------------------------------
# Stdio entry
# ---------------------------------------------------------------------------


async def run() -> None:  # pragma: no cover — exercised via integration / live spawn
    """Serve MCP over stdio. ``python -m axiom.extensions.builtins.mcp.server``."""
    surface = AggregationRegistry.from_node().build()
    server = build_server(surface)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


def main() -> None:  # pragma: no cover
    """Sync wrapper for the ``python -m`` entry point."""
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["build_server", "dispatch_call", "main", "run"]
