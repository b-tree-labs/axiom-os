# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-114 §1 — the identity gate ENFORCED at MCP dispatch.

The matcher/caller/refusal logic is unit-tested in ``test_identity_gate.py``;
here we prove the server actually refuses a non-admitted caller *before* the
handler runs, on both the EC-capable and non-EC dispatch paths, and that an
unmapped tool fails closed to owner-only.
"""
from __future__ import annotations

import asyncio
import json

from datetime import UTC, datetime

from axiom.extensions.builtins.mcp.aggregation import MCPSurface
from axiom.extensions.builtins.mcp.server import _raw_dispatch, dispatch_call
from axiom.infra.principal import local_handle


def _run(coro):
    return asyncio.run(coro)


def _surface(ran: dict, *, allowed: dict[str, tuple[str, ...]]) -> MCPSurface:
    async def _ping(args):
        ran["ping"] = True
        return {"ok": True, "echo": args}

    async def _pong(args):
        ran["pong"] = True
        return {"ok": True}

    return MCPSurface(
        tools=[],
        resources=[],
        prompts=[],
        dispatch={"axiom_probe__ping": _ping, "axiom_probe__pong": _pong},
        content_hash="test",
        generated_at=datetime.now(UTC),
        sources=[],
        allowed_principals=allowed,
    )


# ---- _raw_dispatch: the shared chokepoint --------------------------------


def test_admitted_local_caller_dispatches(monkeypatch):
    monkeypatch.delenv("AXIOM_MCP_CLIENT_PRINCIPAL", raising=False)  # -> local owner
    ran: dict = {}
    surface = _surface(ran, allowed={"axiom_probe__ping": ("@*:local",)})
    out = _run(_raw_dispatch(surface, "axiom_probe__ping", {"text": "hi"}))
    assert ran.get("ping") is True
    assert out == {"ok": True, "echo": {"text": "hi"}}


def test_remote_caller_refused_before_handler(monkeypatch):
    monkeypatch.setenv("AXIOM_MCP_CLIENT_PRINCIPAL", "@svc:acme")
    ran: dict = {}
    surface = _surface(ran, allowed={"axiom_probe__ping": ("@*:local",)})
    out = _run(_raw_dispatch(surface, "axiom_probe__ping", {}))
    assert ran.get("ping") is not True          # handler NEVER ran
    assert out["refused"] is True
    assert "not permitted" in out["error"] and "@svc:acme" in out["error"]


def test_unmapped_tool_fails_closed_to_owner_only(monkeypatch):
    ran: dict = {}
    surface = _surface(ran, allowed={})          # nothing mapped
    # owner (unset env -> local_handle) is admitted by the owner-only default
    monkeypatch.delenv("AXIOM_MCP_CLIENT_PRINCIPAL", raising=False)
    assert _run(_raw_dispatch(surface, "axiom_probe__ping", {})) == {
        "ok": True,
        "echo": {},
    }
    # a remote principal is refused for the same unmapped tool
    ran.clear()
    monkeypatch.setenv("AXIOM_MCP_CLIENT_PRINCIPAL", "@svc:acme")
    out = _run(_raw_dispatch(surface, "axiom_probe__pong", {}))
    assert ran.get("pong") is not True
    assert out["refused"] is True


def test_owner_admitted_matches_local_handle(monkeypatch):
    # The owner-only default is exactly local_handle() — proves the fallback
    # admits the real owner handle, not just because env is unset.
    monkeypatch.setenv("AXIOM_MCP_CLIENT_PRINCIPAL", local_handle())
    ran: dict = {}
    surface = _surface(ran, allowed={})
    assert _run(_raw_dispatch(surface, "axiom_probe__ping", {}))["ok"] is True
    assert ran.get("ping") is True


# ---- dispatch_call: end-to-end wire content, EC-capable path -------------


def test_dispatch_call_refuses_remote_caller_end_to_end(monkeypatch):
    monkeypatch.setenv("AXIOM_MCP_CLIENT_EC_CAPABLE", "true")  # bypass EC gate
    monkeypatch.setenv("AXIOM_MCP_CLIENT_PRINCIPAL", "@svc:acme")
    ran: dict = {}
    surface = _surface(ran, allowed={"axiom_probe__ping": ("@*:local",)})
    content = _run(dispatch_call(surface, "axiom_probe__ping", {}))
    assert ran.get("ping") is not True
    payload = json.loads(content[0].text)
    assert payload["refused"] is True
    assert "@svc:acme" in payload["error"]
