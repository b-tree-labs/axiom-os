# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""MCP-over-HTTP transport — the ``/mcp`` composed mount (RATIONALIZE-6).

The built-in root MCP server (``server.py``) is stdio-only. This adds a
JSON-RPC-over-HTTP transport so MCP joins the composed app's shared socket and
the uniform middleware/auth seam (RATIONALIZE-3): one substrate, one auth, for
HTTP API + registered callbacks + MCP + CLI-RPC — the surface the data-ingestion
solution depends on.

It reuses the existing aggregated :class:`MCPSurface` and ``dispatch_call`` (so
the EC client-capability gate still applies to tool output), exposing them over
a single ``POST /mcp`` JSON-RPC 2.0 endpoint: ``initialize``, ``tools/list``,
``tools/call``, ``resources/list``, ``prompts/list``, ``ping``, and JSON-RPC
notifications (no ``id`` → 202, no envelope).

``build_mcp_router`` is pure: the surface + dispatch function are injected so it
is testable without an aggregated surface, a QueryRouter, or a DB.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

from axiom.extensions.builtins.mcp.aggregation import MCPSurface
from axiom.extensions.builtins.mcp.server import SERVER_INSTRUCTIONS, dispatch_call

# MCP protocol revision advertised at handshake. Matches the SDK's current
# supported revision; clients negotiate down if they are older.
PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "axiom-root"

DispatchFn = Callable[[MCPSurface, str, dict[str, Any]], Awaitable[Any]]

# JSON-RPC 2.0 error codes (subset we emit).
_METHOD_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603


def _dump(obj: Any) -> Any:
    """Serialize an MCP pydantic type (Tool/Resource/Prompt) to JSON-safe dict."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return obj


def _server_version() -> str:
    try:
        from importlib.metadata import version

        return version("axiom")
    except Exception:  # noqa: BLE001 — version is cosmetic at the handshake
        return "0"


def build_mcp_router(
    *,
    surface: MCPSurface,
    dispatch: DispatchFn = dispatch_call,
) -> APIRouter:
    """Build the JSON-RPC-over-HTTP ``/mcp`` router from an injected surface."""
    router = APIRouter()

    async def _handle(method: str, params: dict[str, Any]) -> dict:
        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {"name": SERVER_NAME, "version": _server_version()},
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                "instructions": SERVER_INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [_dump(t) for t in surface.tools]}
        if method == "resources/list":
            return {"resources": [_dump(r) for r in surface.resources]}
        if method == "prompts/list":
            return {"prompts": [_dump(p) for p in surface.prompts]}
        if method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments") or {}
            contents = await dispatch(surface, name, arguments)
            return {
                "content": [_dump(c) for c in contents],
                "isError": False,
            }
        raise _MethodNotFound(method)

    @router.post("/mcp")
    async def mcp_endpoint(body: dict) -> Response:
        rpc_id = body.get("id")
        method = body.get("method", "")
        params = body.get("params") or {}

        # A JSON-RPC notification has no `id`: run side effects, return no
        # envelope. (e.g. notifications/initialized.)
        if rpc_id is None:
            return Response(status_code=202)

        try:
            result = await _handle(method, params)
        except _MethodNotFound as exc:
            return JSONResponse({
                "jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": _METHOD_NOT_FOUND,
                          "message": f"method not found: {exc.method}"},
            })
        except Exception as exc:  # noqa: BLE001 — never crash the transport
            return JSONResponse({
                "jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": _INTERNAL_ERROR, "message": str(exc)[:300]},
            })
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})

    return router


class _MethodNotFound(Exception):
    def __init__(self, method: str) -> None:
        super().__init__(method)
        self.method = method


def mcp_mount_spec():
    """Wire the aggregated node surface and return the ``/mcp`` MountSpec.

    Discovered by the composed app (AEOS ``service`` block). ``requires_authz``
    defaults to True, so MCP rides the uniform authz seam (RATIONALIZE-3) like
    every other mount — no per-transport auth code.
    """
    from axiom.extensions.builtins.http.registry import MountSpec
    from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry

    surface = AggregationRegistry.from_node().build()
    return MountSpec(
        prefix="/mcp",
        router=build_mcp_router(surface=surface),
        extension="mcp",
        bind="127.0.0.1",
        trust_zone="loopback",
    )


__all__ = ["build_mcp_router", "mcp_mount_spec", "PROTOCOL_VERSION"]
