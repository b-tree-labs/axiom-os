# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Server lifecycle tests for the built-in MCP root server.

Spec: ``docs/specs/spec-builtin-mcp-server.md`` §5 + §12.3.

We don't spin a real subprocess in unit tests (that lives in
``integration_tests/test_stdio_roundtrip.py``). These tests cover:

- ``build_server`` returns a configured ``mcp.server.Server`` instance.
- ``list_tools``, ``call_tool``, ``list_resources``, ``list_prompts`` are
  registered on the server.
- An unknown ``call_tool`` returns a structured error rather than raising.
- An ``axi mcp status`` invocation returns surface info.
- An ``axi mcp list-tools`` invocation returns the tool list.
"""

from __future__ import annotations

import asyncio
import json

from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry
from axiom.extensions.builtins.mcp.server import build_server

# ---------------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------------


def test_build_server_returns_mcp_server(tmp_axiom_home):
    """``build_server`` returns an instance of ``mcp.server.Server`` named axiom-root."""
    from mcp.server import Server

    surface = AggregationRegistry(extensions=[]).build()
    srv = build_server(surface)
    assert isinstance(srv, Server)
    assert getattr(srv, "name", None) == "axiom-root"


def test_server_instructions_present(tmp_axiom_home):
    """The server carries an ``instructions`` payload visible to MCP clients at handshake.

    Per ADR-052 + prd-builtin-mcp-server §5.2: every peer harness (Claude
    Code, Cursor, Cline, Goose, ChatGPT Desktop, Claude.ai, Codex, …) sees
    the load-bearing platform conventions on connect — schema-per-extension
    via ``session_for``, cross-extension reads via the data platform, the
    within-extension tenancy menu.
    """
    from axiom.extensions.builtins.mcp.server import SERVER_INSTRUCTIONS

    surface = AggregationRegistry(extensions=[]).build()
    srv = build_server(surface)
    # The mcp SDK stores instructions on the constructed Server; surface
    # name varies across SDK versions — check both common attribute names.
    instructions = getattr(srv, "instructions", None) or getattr(
        getattr(srv, "_init_options", None), "instructions", None
    )
    assert instructions == SERVER_INSTRUCTIONS


def test_server_instructions_name_load_bearing_concepts():
    """The instructions string names the concepts agents must respect.

    Tests are calibrated to the substance, not the prose — if the wording
    is rewritten, these checks survive as long as the concepts are still
    named.
    """
    from axiom.extensions.builtins.mcp.server import SERVER_INSTRUCTIONS

    for term in ("session_for", "ADR-052", "ADR-049", "public", "schema"):
        assert term in SERVER_INSTRUCTIONS, f"missing load-bearing term: {term}"


def test_call_tool_unknown_returns_structured_error(tmp_axiom_home):
    """An unknown tool produces ``[TextContent]`` with a structured error JSON."""
    from axiom.extensions.builtins.mcp.server import dispatch_call

    surface = AggregationRegistry(extensions=[]).build()
    out = asyncio.run(dispatch_call(surface, "does_not_exist", {}))
    # dispatch_call returns the same wire shape the MCP handler would.
    assert isinstance(out, list)
    assert len(out) == 1
    payload = json.loads(out[0].text)
    assert "error" in payload
    assert "does_not_exist" in payload["error"]


def test_call_tool_dispatch_to_platform_handler(tmp_axiom_home):
    """``axiom_node__hooks_list`` dispatches and returns valid JSON content."""
    from axiom.extensions.builtins.mcp.server import dispatch_call

    surface = AggregationRegistry(extensions=[]).build()
    out = asyncio.run(dispatch_call(surface, "axiom_node__hooks_list", {}))
    assert isinstance(out, list)
    assert len(out) == 1
    payload = json.loads(out[0].text)
    assert "hooks" in payload


def test_call_tool_handler_exception_translated(tmp_axiom_home):
    """A handler that raises produces ``{"error": "<Type>: <msg>"}``."""
    import dataclasses

    from axiom.extensions.builtins.mcp.server import dispatch_call

    base = AggregationRegistry(extensions=[]).build()

    async def boom(_args):
        raise RuntimeError("kaboom")

    new_dispatch = dict(base.dispatch)
    new_dispatch["broken_tool"] = boom
    surface = dataclasses.replace(base, dispatch=new_dispatch)

    out = asyncio.run(dispatch_call(surface, "broken_tool", {}))
    payload = json.loads(out[0].text)
    assert "error" in payload
    assert "RuntimeError" in payload["error"]
    assert "kaboom" in payload["error"]


# ---------------------------------------------------------------------------
# CLI surface — status + list-tools subcommands
# ---------------------------------------------------------------------------


def test_axi_mcp_status_returns_surface_summary(tmp_axiom_home, capsys):
    """``axi mcp status`` prints a surface summary including counts + hash."""
    from axiom.extensions.builtins.mcp.cli import main

    rc = main(["status"])
    assert rc == 0
    output = capsys.readouterr().out
    assert "tools" in output.lower()
    assert "hash" in output.lower()


def test_axi_mcp_list_tools_lists_platform_tools(tmp_axiom_home, capsys):
    """``axi mcp list-tools`` lists every platform tool name."""
    from axiom.extensions.builtins.mcp.cli import main

    rc = main(["list-tools"])
    assert rc == 0
    output = capsys.readouterr().out
    assert "axiom_memory__compose" in output
    assert "axiom_node__hooks_list" in output


def test_axi_mcp_inspect_tool(tmp_axiom_home, capsys):
    """``axi mcp inspect <tool>`` prints tool metadata."""
    from axiom.extensions.builtins.mcp.cli import main

    rc = main(["inspect", "axiom_node__hooks_list"])
    assert rc == 0
    output = capsys.readouterr().out
    assert "axiom_node__hooks_list" in output


def test_axi_mcp_inspect_unknown_tool_errors(tmp_axiom_home, capsys):
    """``axi mcp inspect <unknown>`` exits non-zero with a clear message."""
    from axiom.extensions.builtins.mcp.cli import main

    rc = main(["inspect", "does_not_exist"])
    assert rc != 0


def test_axi_mcp_regenerate_writes_cache(tmp_axiom_home, capsys):
    """``axi mcp regenerate`` writes ``surface.json`` under ``$AXIOM_HOME/mcp/``."""
    from axiom.extensions.builtins.mcp.cli import main

    rc = main(["regenerate"])
    assert rc == 0
    cache = tmp_axiom_home / "mcp" / "surface.json"
    assert cache.exists()


def test_axi_mcp_clients_lists_supported(tmp_axiom_home, capsys):
    """``axi mcp clients`` renders the harness × EC-routing capability chart."""
    from axiom.extensions.builtins.mcp.cli import main

    rc = main(["clients"])
    assert rc == 0
    output = capsys.readouterr().out.lower()
    # Canonical harness names + the EC-routing column.
    assert "claude-code" in output
    assert "cursor" in output
    assert "ec-routable" in output


def test_axi_mcp_clients_json(tmp_axiom_home, capsys):
    """``axi mcp clients --json`` emits the structured capability matrix."""
    from axiom.extensions.builtins.mcp.cli import main

    rc = main(["clients", "--json"])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    by = {r["client"]: r for r in rows}
    assert by["cursor"]["ec_routable"] is False  # cloud-proxied
    assert by["claude-code"]["ec_routable"] is True
    assert by["vscode"]["ec_routable"] is True  # Copilot BYOK / Continue


# ---------------------------------------------------------------------------
# EC client-capability gate (server dispatch chokepoint)
# ---------------------------------------------------------------------------


def _stub_ec_router(monkeypatch):
    """Force dispatch_call's QueryRouter to classify everything export_controlled."""
    import axiom.llm.router as routermod
    from axiom.infra.router import RoutingDecision, RoutingTier

    class _EC:
        def classify(self, text, **kw):
            return RoutingDecision(
                tier=RoutingTier.EXPORT_CONTROLLED,
                reason="stub EC", classifier="keyword", matched_terms=["ITAR"],
            )

    monkeypatch.setattr(routermod, "QueryRouter", _EC)


def test_gate_withholds_ec_result_for_non_ec_client(tmp_axiom_home, monkeypatch):
    """env unset/false → non-EC client → EC tool output is WITHHELD."""
    from axiom.extensions.builtins.mcp.server import dispatch_call

    monkeypatch.delenv("AXIOM_MCP_CLIENT_EC_CAPABLE", raising=False)
    monkeypatch.setenv("AXIOM_MCP_CLIENT", "cursor")
    _stub_ec_router(monkeypatch)

    surface = AggregationRegistry(extensions=[]).build()
    out = asyncio.run(dispatch_call(surface, "axiom_node__hooks_list", {}))
    payload = json.loads(out[0].text)
    assert "withheld" in payload.get("error", "").lower()
    assert payload["routing"]["refused"] is True


def test_gate_bypassed_for_ec_capable_client(tmp_axiom_home, monkeypatch):
    """env true → EC-capable client → result returned, classifier never run."""
    from axiom.extensions.builtins.mcp.server import dispatch_call

    monkeypatch.setenv("AXIOM_MCP_CLIENT_EC_CAPABLE", "true")
    # If the gate were active it'd classify EC and withhold; bypass means it won't.
    _stub_ec_router(monkeypatch)

    surface = AggregationRegistry(extensions=[]).build()
    out = asyncio.run(dispatch_call(surface, "axiom_node__hooks_list", {}))
    payload = json.loads(out[0].text)
    assert "withheld" not in json.dumps(payload).lower()
