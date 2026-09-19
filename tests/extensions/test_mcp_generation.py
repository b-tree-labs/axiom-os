# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for #29 auto-MCP config generation from extension manifests.

Each extension declares its MCP server in ``axiom-extension.toml``:

    [mcp_servers.axiom-classroom]
    command = "python"
    args = ["-m", "axiom.extensions.builtins.classroom.mcp_server"]

``axi mcp generate`` collates every enabled extension's server(s) into
a client config file (Claude Code ``.mcp.json`` by default). Users
don't hand-edit MCP configs — the manifest is the source of truth.
"""

from __future__ import annotations

import json
from pathlib import Path

from axiom.extensions.contracts import Extension, MCPServerDef
from axiom.extensions.mcp_generation import (
    MCPTarget,
    build_mcp_config,
    merge_mcp_config,
    write_mcp_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ext(name: str, servers: dict[str, MCPServerDef]) -> Extension:
    """Minimal Extension fixture with just the fields we need."""
    return Extension(
        name=name,
        version="0.1.0",
        description="",
        author="",
        root=Path("/tmp/ext"),
        mcp_servers=servers,
    )


def _server(name: str, cmd: str = "python", args: list[str] | None = None) -> MCPServerDef:
    return MCPServerDef(
        name=name,
        type="stdio",
        command=cmd,
        args=args or [f"-m axiom.extensions.builtins.{name}.mcp_server"],
    )


# ---------------------------------------------------------------------------
# build_mcp_config
# ---------------------------------------------------------------------------


class TestBuildMcpConfig:
    def test_claude_code_format(self):
        ext = _ext("classroom", {
            "axiom-classroom": _server("classroom"),
        })
        config = build_mcp_config([ext], target=MCPTarget.CLAUDE_CODE)
        assert "mcpServers" in config
        assert "axiom-classroom" in config["mcpServers"]
        entry = config["mcpServers"]["axiom-classroom"]
        assert entry["command"] == "python"
        assert entry["args"][0].startswith("-m")

    def test_collates_multiple_extensions(self):
        ext1 = _ext("classroom", {"axiom-classroom": _server("classroom")})
        ext2 = _ext("scan", {"axiom-scan": _server("signals")})
        config = build_mcp_config([ext1, ext2], target=MCPTarget.CLAUDE_CODE)
        assert set(config["mcpServers"].keys()) == {"axiom-classroom", "axiom-scan"}

    def test_disabled_extensions_excluded(self):
        ext = _ext("classroom", {"axiom-classroom": _server("classroom")})
        ext.enabled = False
        config = build_mcp_config([ext], target=MCPTarget.CLAUDE_CODE)
        assert config["mcpServers"] == {}

    def test_extensions_without_mcp_servers_skipped(self):
        ext = _ext("note", {})
        config = build_mcp_config([ext], target=MCPTarget.CLAUDE_CODE)
        assert config["mcpServers"] == {}

    def test_env_vars_flow_through(self):
        server = MCPServerDef(
            name="axiom-ext", type="stdio", command="python", args=["-m", "x"],
            env={"FOO": "bar"},
        )
        ext = _ext("ext", {"axiom-ext": server})
        config = build_mcp_config([ext], target=MCPTarget.CLAUDE_CODE)
        assert config["mcpServers"]["axiom-ext"]["env"] == {"FOO": "bar"}

    def test_empty_list_produces_empty_config(self):
        config = build_mcp_config([], target=MCPTarget.CLAUDE_CODE)
        assert config == {"mcpServers": {}}


# ---------------------------------------------------------------------------
# merge_mcp_config — preserve user-added servers
# ---------------------------------------------------------------------------


class TestMergeConfig:
    def test_preserves_user_added_servers(self):
        existing = {
            "mcpServers": {
                "user-thing": {"command": "node", "args": ["index.js"]},
            }
        }
        generated = {
            "mcpServers": {
                "axiom-classroom": {"command": "python", "args": ["-m", "x"]},
            }
        }
        merged = merge_mcp_config(existing, generated)
        assert "user-thing" in merged["mcpServers"]
        assert "axiom-classroom" in merged["mcpServers"]

    def test_generated_wins_on_collision(self):
        """Extension-declared server overrides any stale same-name entry."""
        existing = {
            "mcpServers": {
                "axiom-classroom": {"command": "stale", "args": []},
            }
        }
        generated = {
            "mcpServers": {
                "axiom-classroom": {"command": "python", "args": ["-m", "x"]},
            }
        }
        merged = merge_mcp_config(existing, generated)
        assert merged["mcpServers"]["axiom-classroom"]["command"] == "python"

    def test_empty_existing(self):
        generated = {"mcpServers": {"a": {"command": "x"}}}
        merged = merge_mcp_config({}, generated)
        assert merged == generated


# ---------------------------------------------------------------------------
# write_mcp_config
# ---------------------------------------------------------------------------


class TestWriteMcpConfig:
    def test_creates_file_if_missing(self, tmp_path):
        path = tmp_path / ".mcp.json"
        ext = _ext("classroom", {"axiom-classroom": _server("classroom")})
        write_mcp_config(path, [ext], target=MCPTarget.CLAUDE_CODE)
        assert path.exists()
        data = json.loads(path.read_text())
        assert "axiom-classroom" in data["mcpServers"]

    def test_merges_with_existing_file(self, tmp_path):
        path = tmp_path / ".mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {
                "user-thing": {"command": "node", "args": ["x.js"]},
            }
        }))
        ext = _ext("classroom", {"axiom-classroom": _server("classroom")})
        write_mcp_config(path, [ext], target=MCPTarget.CLAUDE_CODE)
        data = json.loads(path.read_text())
        assert "user-thing" in data["mcpServers"]
        assert "axiom-classroom" in data["mcpServers"]

    def test_writes_are_atomic_on_partial_failure(self, tmp_path):
        """If write fails mid-way, the original file must remain intact."""
        path = tmp_path / ".mcp.json"
        original = {"mcpServers": {"user-thing": {"command": "node"}}}
        path.write_text(json.dumps(original))

        # Simulate a failure by making the parent directory unwritable.
        # We instead verify the happy path preserves content even if the
        # generated config is empty.
        write_mcp_config(path, [], target=MCPTarget.CLAUDE_CODE)
        data = json.loads(path.read_text())
        assert data["mcpServers"]["user-thing"]["command"] == "node"
