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
import os
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListPromptsResult,
    ListResourcesResult,
    ListToolsResult,
    TextContent,
)

from axiom.extensions.builtins.mcp import identity_gate
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


def instructions_with_discovery(
    tools: Any, *, state_dir: Any = None, principal: str | None = None
) -> str:
    """:data:`SERVER_INSTRUCTIONS` plus what this install has not been used for.

    People do not reach for what they do not know exists, and the block that
    fixes that has to arrive without being obtrusive. The handshake is the
    cheapest possible place: every client reads ``instructions`` once at
    connect, no file is created in anybody's repository, and nothing is written
    to disk.

    The block is the GAP — installed minus used — so using a capability removes
    its line, the block shrinks as discovery succeeds, and an install where
    everything is already in use gets the baseline string back byte-identical.
    Nobody writes prose at either end of that loop.

    The platform conventions always come first. Discovery is an addition; a
    block that displaced them would trade a real instruction for an
    advertisement.

    Declining the telemetry series does NOT silence this. Wiring the two
    together would leave the person who opted out of measurement the least able
    to find the platform, which is the opposite of the point — their block
    simply never shrinks.

    Never raises: attaching a client must not depend on a readable series.
    """
    try:
        from pathlib import Path as _Path

        from axiom.infra.capability_telemetry import (
            default_state_dir,
            discovered_capabilities,
        )
        from axiom.memory.rendering import (
            capabilities_for_block,
            render_capability_block,
        )

        resolved = _Path(state_dir) if state_dir is not None else default_state_dir()
        used = discovered_capabilities(state_dir=resolved, principal=principal)
        block = render_capability_block(
            capabilities_for_block(list(tools), used=used)
        )
    except Exception:  # noqa: BLE001 — discovery never blocks a connection
        return SERVER_INSTRUCTIONS
    if not block:
        return SERVER_INSTRUCTIONS
    return f"{SERVER_INSTRUCTIONS}\n\n{block}"


def build_server(surface: MCPSurface) -> Server:
    """Wire an ``mcp.server.Server`` against the given surface.

    The handlers are closures over ``surface`` so a fresh build for each
    subprocess is straightforward.

    The server's ``instructions`` payload is set to ``SERVER_INSTRUCTIONS`` so
    every connecting client sees the load-bearing platform conventions at the
    protocol handshake.
    """
    # Arm the capability series: this surface is where an assistant's calls
    # arrive, so it is the one place "did the platform get used this session"
    # is answerable. Idempotent, honours AXIOM_CAPABILITY_TELEMETRY, and never
    # blocks the server coming up.
    try:
        from axiom.infra.capability_telemetry import enable_capability_telemetry

        enable_capability_telemetry()
    except Exception:  # noqa: BLE001 — an observation never blocks the server
        pass

    async def _list_tools(_ctx: Any, _params: Any = None) -> ListToolsResult:
        return ListToolsResult(tools=list(surface.tools))

    async def _call_tool(_ctx: Any, params: CallToolRequestParams) -> CallToolResult:
        content = await dispatch_call(surface, params.name, params.arguments or {})
        return CallToolResult(content=list(content))

    async def _list_resources(_ctx: Any, _params: Any = None) -> ListResourcesResult:
        return ListResourcesResult(resources=list(surface.resources))

    async def _list_prompts(_ctx: Any, _params: Any = None) -> ListPromptsResult:
        return ListPromptsResult(prompts=list(surface.prompts))

    server: Server = Server(
        "axiom-root",
        instructions=instructions_with_discovery(surface.tools),
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
        on_list_resources=_list_resources,
        on_list_prompts=_list_prompts,
    )

    return server


# ---------------------------------------------------------------------------
# Dispatch — separated so it can be exercised in unit tests without spawning
# the SDK's request loop
# ---------------------------------------------------------------------------


# --- EC client-capability gate ----------------------------------------------
# The host client that spawned this server may run a public-cloud model (e.g.
# Cursor proxies inference through its own backend). Such a client is an
# exfiltration sink: EC tool *output* returned to it leaves the boundary. The
# installer stamps the client's EC-capability into the env when it registers
# the server (a client is EC-capable only when its model is the in-enclave
# endpoint). When the client is NOT EC-capable, we run the tool locally but
# withhold any result that classifies export-controlled.


def _client_ec_capable() -> bool:
    """Read the installer-stamped EC-capability. Fail CLOSED: anything other
    than an explicit 'true' (incl. unset) is treated as non-EC-capable."""
    return os.environ.get("AXIOM_MCP_CLIENT_EC_CAPABLE", "").strip().lower() == "true"


def _client_name() -> str:
    return os.environ.get("AXIOM_MCP_CLIENT", "unknown")


async def _raw_dispatch(surface: MCPSurface, name: str, arguments: dict[str, Any]) -> Any:
    """Run a tool handler and return its raw Python result (or an error dict)."""
    handler = surface.dispatch.get(name)
    if handler is None:
        return {"error": f"unknown tool: {name}"}
    # Identity gate (ADR-114 §1): the *who* control. The caller principal must
    # match this tool's allowed_principals before the handler runs; an unmapped
    # tool fails closed to owner-only. Distinct from and prior to the GUARD /
    # site-rule *whether* control that the handler's invoke_capability consults.
    reason = identity_gate.refusal_reason(
        name,
        identity_gate.caller_principal(),
        surface.allowed_principals.get(name, ()),
    )
    if reason:
        return {"error": reason, "refused": True}
    try:
        return await handler(arguments)
    except Exception as exc:  # noqa: BLE001 — translate every error
        return {"error": f"{type(exc).__name__}: {exc}"}


async def dispatch_call(
    surface: MCPSurface, name: str, arguments: dict[str, Any]
) -> list[TextContent]:
    """Run a tool handler against the given surface; return MCP wire content.

    For a non-EC-capable host client, every result passes through the
    classification gate (``gate_result_for_client``) and EC output is withheld.
    For an EC-capable client the gate is bypassed (zero overhead). Either way,
    a single bad call never hard-crashes the client.
    """
    if _client_ec_capable():
        result = await _raw_dispatch(surface, name, arguments)
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    # Non-EC-capable client: gate the result through the router.
    from axiom.extensions.builtins.mcp.routing import gate_result_for_client
    from axiom.llm.router import QueryRouter

    async def _disp(n: str, a: dict[str, Any]) -> Any:
        return await _raw_dispatch(surface, n, a)

    try:
        envelope = await gate_result_for_client(
            tool_name=name,
            arguments=arguments,
            dispatcher=_disp,
            router=QueryRouter(),
            client_ec_capable=False,
            client_name=_client_name(),
        )
    except Exception as exc:  # noqa: BLE001 — never hard-crash the client
        payload = {"error": f"{type(exc).__name__}: {exc}"}
        return [TextContent(type="text", text=json.dumps(payload))]

    if "result" in envelope:
        out = envelope["result"]
    else:
        # Withheld — surface the refusal (routing breadcrumb), no result.
        out = {
            "error": "export-controlled content withheld from a non-EC-capable client",
            "routing": envelope.get("routing", {}),
        }
    return [TextContent(type="text", text=json.dumps(out, indent=2, default=str))]


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
