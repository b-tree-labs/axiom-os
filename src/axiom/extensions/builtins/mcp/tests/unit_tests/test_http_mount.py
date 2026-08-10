# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for the MCP-over-HTTP transport mount (RATIONALIZE-6).

The built-in root MCP server was stdio-only. This adds a JSON-RPC-over-HTTP
transport as a /mcp MountSpec so MCP joins the composed app's shared socket +
uniform middleware/auth — the last of the "many integrations on one substrate"
surfaces (HTTP API, callbacks, MCP, CLI-RPC).

build_mcp_router is pure: the surface + the dispatch function are injected, so
these tests need no aggregated surface, no QueryRouter, and no DB.
"""

from __future__ import annotations

import pytest

from mcp.types import TextContent, Tool


def _surface(tools=None, dispatch=None):
    from datetime import datetime, timezone

    from axiom.extensions.builtins.mcp.aggregation import MCPSurface

    return MCPSurface(
        tools=tools or [],
        resources=[],
        prompts=[],
        dispatch=dispatch or {},
        content_hash="test",
        generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        sources=[],
    )


def _client(surface, dispatch_fn):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.http.server import create_app
    from axiom.extensions.builtins.mcp.http_mount import build_mcp_router

    app = create_app(title="t", version="0", description="")
    app.include_router(build_mcp_router(surface=surface, dispatch=dispatch_fn))
    return TestClient(app)


async def _never(*a, **k):  # dispatch that must not be called
    raise AssertionError("dispatch should not be called")


def test_initialize_returns_serverinfo_and_instructions():
    client = _client(_surface(), _never)
    resp = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == 1
    result = body["result"]
    assert result["serverInfo"]["name"] == "axiom-root"
    assert "protocolVersion" in result
    assert "instructions" in result and result["instructions"]


def test_tools_list_serializes_surface_tools():
    tool = Tool(name="echo", description="echoes", inputSchema={"type": "object"})
    client = _client(_surface(tools=[tool]), _never)
    resp = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    body = resp.json()
    names = [t["name"] for t in body["result"]["tools"]]
    assert names == ["echo"]


def test_tools_call_dispatches_and_wraps_content():
    seen = {}

    async def dispatch_fn(surface, name, arguments):
        seen["name"] = name
        seen["args"] = arguments
        return [TextContent(type="text", text='{"ok": true}')]

    client = _client(_surface(), dispatch_fn)
    resp = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "echo", "arguments": {"x": 1}}})
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    assert result["content"][0]["text"] == '{"ok": true}'
    assert seen["name"] == "echo" and seen["args"] == {"x": 1}


def test_unknown_method_returns_jsonrpc_error():
    client = _client(_surface(), _never)
    resp = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 4, "method": "bogus/thing", "params": {}})
    body = resp.json()
    assert "result" not in body
    assert body["error"]["code"] == -32601  # method not found


def test_notification_returns_no_body():
    """A JSON-RPC notification (no id) gets a 202 and no JSON-RPC envelope."""
    client = _client(_surface(), _never)
    resp = client.post("/mcp", json={
        "jsonrpc": "2.0", "method": "notifications/initialized"})
    assert resp.status_code == 202


def test_mount_spec_is_authz_gated():
    """The mount declares requires_authz=True so MCP rides the uniform seam."""
    from axiom.extensions.builtins.mcp import http_mount

    spec = http_mount.mcp_mount_spec()
    assert spec.prefix == "/mcp"
    assert spec.requires_authz is True
