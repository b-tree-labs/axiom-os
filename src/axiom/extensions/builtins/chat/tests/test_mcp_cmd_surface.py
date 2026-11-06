# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``/mcp``: the MCP surface as implemented, from the same files ``mcp status`` reads.

Two halves, kept apart because they answer different questions: which MCP servers Axiom *provides* and
which harnesses they are registered with, and whether the chat agent can *call* MCP servers (it can only
if a tool in its table is backed by an MCP client; the command checks the table rather than asserting).
"""

from __future__ import annotations

import sys

import pytest

from axiom.extensions.builtins.chat import tools
from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded
from axiom.extensions.builtins.chat.command_registry import REGISTRY
from axiom.extensions.builtins.mcp import install
from axiom.infra.orchestrator.actions import ActionCategory

from ._tools_cmd_helpers import run

SECRET = "tok-9f8e7d6c5b4a-do-not-print"


@pytest.fixture(autouse=True)
def harness_home(tmp_path, monkeypatch):
    """Every harness config points into tmp, and HOME is tmp: nothing reads the developer's real setup."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for spec in install.TOOL_SPECS:
        monkeypatch.setenv(spec.env_override, str(tmp_path / "cfg" / f"{spec.name}.json"))
    monkeypatch.setenv("AXIOM_HOME", str(tmp_path / "axiom-home"))
    # Walking every installed extension's manifest takes seconds; the scanners stay real, their roots empty.
    from axiom.extensions import discovery

    monkeypatch.setattr(tools, "_manifest_roots", lambda: [])
    monkeypatch.setattr(discovery, "discover_and_load_chat_tools", lambda *a, **k: [])
    return home


def _register(tool: str, server: str, *, command: str | None = None, env=None):
    spec = install.spec_for(tool)
    path = install.config_path_for(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    install.install_entry(
        spec, server, command or sys.executable, ["-m", "x"], env or {}, path=path
    )


def _row(out: str, harness: str) -> str:
    rows = [ln for ln in out.splitlines() if ln.strip().startswith(harness + " ")]
    assert rows, f"{harness} missing from:\n{out}"
    return rows[0]


def test_registered_in_the_registry():
    ensure_loaded()
    c = REGISTRY.get("/mcp")
    assert c is not None and "MCP" in c.summary


def test_names_the_one_server_axiom_provides_and_says_memory_is_part_of_it():
    out = run("/mcp")
    assert install.resolve_server()[0] in out
    assert "memory included" in out
    assert "memory only" not in out, (
        "the standalone memory server is retired; do not report a second one"
    )


def test_every_supported_harness_appears_with_its_ec_routability(harness_home):
    out = run("/mcp")
    caps = {c["client"]: c for c in install.client_capabilities()}
    for name, cap in caps.items():
        row = _row(out, name)
        assert ("routable" if cap["ec_routable"] else "blocked") in row, row
    # the matrix distinguishes harnesses; so must the output
    assert "blocked" in out and "routable" in out


def test_a_harness_registered_with_the_composed_server_is_reported_registered():
    _register("claude-code", install.resolve_server()[0])
    row = _row(run("/mcp"), "claude-code")
    assert "registered" in row


def test_a_harness_with_nothing_registered_is_not_reported_registered():
    row = _row(run("/mcp"), "cursor")
    assert "registered" not in row.replace("not registered", "")


def test_each_harness_row_has_one_registration_state_for_the_one_server():
    _register("cursor", install.resolve_server()[0])
    row = _row(run("/mcp"), "cursor")
    cells = [c for c in row.split("  ") if c.strip()]
    assert cells.count("registered") == 1, row


def test_a_broken_command_is_called_out_with_its_remedy(tmp_path):
    _register("claude-code", install.resolve_server()[0], command=str(tmp_path / "gone" / "python"))
    out = run("/mcp")
    assert "does not exist" in out and "mcp install" in out


def test_tokens_in_a_server_env_block_are_never_printed():
    _register("claude-code", install.resolve_server()[0], env={"API_TOKEN": SECRET})
    out = run("/mcp")
    assert SECRET not in out
    assert "API_TOKEN" not in out


def test_command_lines_are_not_dumped():
    _register("claude-code", install.resolve_server()[0])
    out = run("/mcp")
    assert " -m x" not in out


def test_the_chat_agent_is_reported_unable_to_call_mcp_servers_when_its_table_has_no_mcp_tool():
    out = run("/mcp")
    assert "cannot call" in out.lower()
    assert "not an MCP client" in out or "no MCP client" in out


def test_that_claim_is_checked_against_the_table_not_asserted(monkeypatch):
    """If a tool backed by an MCP client appears in the table, the command must say so."""
    from axiom.extensions.builtins.chat.tools import ToolDef
    from axiom.extensions.builtins.mcp import cli as mcp_cli

    real = tools.get_all_tools()
    real["ext_call"] = ToolDef(
        name="ext_call",
        description="calls out",
        category=ActionCategory.READ,
        handler=mcp_cli._cmd_status,  # a callable that lives in the mcp package
    )
    monkeypatch.setattr(tools, "get_all_tools", lambda: real)
    out = run("/mcp")
    assert "ext_call" in out
    assert "cannot call" not in out.lower()


def test_points_to_the_authoritative_cli_with_the_brand_name(monkeypatch):
    from axiom.infra import branding

    class _B:
        cli_name = "zed"

    monkeypatch.setattr(branding, "get_branding", lambda: _B())
    out = run("/mcp")
    assert "zed mcp status" in out and "axi mcp" not in out
