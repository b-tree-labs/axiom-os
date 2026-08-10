# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the axiom-memory MCP server.

Pure-python tool functions are tested directly (the stdio wiring is
smoke-tested separately). Read tools (show, recent, search) and the
write tool (append) all hit the same isolated CompositionService.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def isolated_composition(tmp_path: Path):
    """Tmp-path-isolated CompositionService — no pollution of ~/.axi/memory/."""
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    base = tmp_path / "memory"
    base.mkdir()
    kp = generate_keypair()
    reg = ArtifactRegistry(backend=SQLiteBackend(base / "artifacts.db"))
    audit = AuditLog(base / "audit.jsonl", signing_keypair=kp)
    return CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


@pytest.fixture
def patched_composition(monkeypatch, isolated_composition):
    """Monkeypatch _build_default_composition so MCP tools see the isolated service."""
    from axiom.extensions.builtins.memory import mcp_server
    monkeypatch.setattr(
        mcp_server, "_build_default_composition", lambda: isolated_composition,
    )
    return isolated_composition


# ---------------------------------------------------------------------------
# list_tools — schema contract
# ---------------------------------------------------------------------------


def test_tool_list_includes_read_and_write_tools():
    from axiom.extensions.builtins.memory import mcp_server

    tool_names = {t.name for t in mcp_server._TOOLS}

    # Read tools
    assert "axiom_memory_show" in tool_names
    assert "axiom_memory_recent" in tool_names
    assert "axiom_memory_search" in tool_names

    # Write tools
    assert "axiom_memory_append" in tool_names


def test_append_tool_input_schema_requires_tool_only():
    """principal_id became optional once memory.default_principal pin landed —
    only `tool` is required at the schema level. Models can still pass
    principal_id explicitly to override the pinned default.
    """
    from axiom.extensions.builtins.memory import mcp_server

    append_tool = next(
        t for t in mcp_server._TOOLS if t.name == "axiom_memory_append"
    )
    schema = append_tool.inputSchema
    required = set(schema.get("required", []))
    assert "tool" in required
    # principal_id is in properties (still acceptable) but not required.
    props = schema["properties"]
    assert "principal_id" in props
    assert "user_input" in props
    assert "assistant_output" in props


# ---------------------------------------------------------------------------
# Server initialization carries instructions for model discipline
# ---------------------------------------------------------------------------


def test_server_initialization_options_include_instructions():
    from axiom.extensions.builtins.memory import mcp_server

    instructions = mcp_server.INSTRUCTIONS
    assert isinstance(instructions, str)
    assert len(instructions) > 100  # not a stub
    # The instructions must drive model discipline on append.
    assert "memory" in instructions.lower()
    assert "append" in instructions.lower()


# ---------------------------------------------------------------------------
# axiom_memory_append — the write tool
# ---------------------------------------------------------------------------


def test_append_persists_via_composition_service(patched_composition):
    from axiom.extensions.builtins.memory import mcp_server
    from axiom.memory.session_summary import list_fragments_by_principal

    result = mcp_server.append(
        principal_id="user@example.org",
        tool="claude-code",
        model="opus-4-7",
        user_input="What's k_eff?",
        assistant_output="Effective multiplication factor.",
    )

    assert result["fragment_id"]
    assert result["principal_id"] == "user@example.org"

    # Fragment really lands in the ledger and round-trips through show path.
    frags = list_fragments_by_principal(
        patched_composition, "user@example.org", limit=10,
    )
    assert len(frags) == 1
    assert frags[0].id == result["fragment_id"]


def test_append_handles_user_input_only(patched_composition):
    """Partial turns are valid — model may append before producing output."""
    from axiom.extensions.builtins.memory import mcp_server

    result = mcp_server.append(
        principal_id="user@example.org",
        tool="claude-code",
        user_input="Asking something long...",
        assistant_output="",
    )
    assert result["fragment_id"]


# ---------------------------------------------------------------------------
# Read tools — show / recent / search
# ---------------------------------------------------------------------------


def test_show_returns_fragments_for_principal(patched_composition):
    from axiom.extensions.builtins.memory import mcp_server

    mcp_server.append(
        principal_id="user@example.org", tool="claude-code",
        user_input="A", assistant_output="B",
    )
    mcp_server.append(
        principal_id="user@example.org", tool="chatgpt",
        user_input="C", assistant_output="D",
    )

    out = mcp_server.show(principal_id="user@example.org", limit=10)
    assert out["principal"] == "user@example.org"
    assert out["fragment_count"] == 2


def test_recent_returns_n_most_recent(patched_composition):
    from axiom.extensions.builtins.memory import mcp_server

    for i in range(5):
        mcp_server.append(
            principal_id="user@example.org", tool="claude-code",
            user_input=f"q{i}", assistant_output=f"a{i}",
        )

    out = mcp_server.recent(principal_id="user@example.org", n=3)
    assert len(out["fragments"]) == 3


def test_search_filters_by_tool(patched_composition):
    from axiom.extensions.builtins.memory import mcp_server

    mcp_server.append(
        principal_id="user@example.org", tool="claude-code",
        user_input="x", assistant_output="y",
    )
    mcp_server.append(
        principal_id="user@example.org", tool="chatgpt",
        user_input="x", assistant_output="y",
    )
    mcp_server.append(
        principal_id="user@example.org", tool="claude-code",
        user_input="x", assistant_output="y",
    )

    out = mcp_server.search(
        principal_id="user@example.org", tool="claude-code",
    )
    assert len(out["fragments"]) == 2

    out = mcp_server.search(
        principal_id="user@example.org", tool="chatgpt",
    )
    assert len(out["fragments"]) == 1


def test_search_filters_by_query_substring(patched_composition):
    from axiom.extensions.builtins.memory import mcp_server

    mcp_server.append(
        principal_id="user@example.org", tool="claude-code",
        user_input="sensor calibration", assistant_output="threshold explained",
    )
    mcp_server.append(
        principal_id="user@example.org", tool="claude-code",
        user_input="lunch ideas", assistant_output="tacos",
    )

    out = mcp_server.search(principal_id="user@example.org", query="calibration")
    assert len(out["fragments"]) == 1
    assert "calibration" in out["fragments"][0]["user_input"].lower()


# ---------------------------------------------------------------------------
# Server build — smoke test (no transport)
# ---------------------------------------------------------------------------


def test_build_server_returns_server_instance():
    from axiom.extensions.builtins.memory import mcp_server

    server = mcp_server.build_server()
    assert server is not None
    # MCP Server objects expose name attribute.
    assert "memory" in server.name.lower()
