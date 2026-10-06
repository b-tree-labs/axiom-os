# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""MCP registrars for the GUI harnesses — Cursor, VS Code, Claude desktop.

The ledger is only cross-tool if every harness can reach it. Claude Code
and Codex already had registrars; these three are the remaining ones on
this machine whose config format can be verified rather than guessed.

Two shapes exist in practice:

- **``mcpServers`` + bare command/args** — Cursor, Claude desktop, and
  Claude Code all use this.
- **``servers`` + an explicit ``type``** — VS Code, which also keeps an
  ``inputs`` array alongside for its own prompt plumbing.

Every registrar has the same obligations: write idempotently, preserve
every other server the user already configured, and never rewrite a
config file it could not parse — a harness config is the user's, and
clobbering it to add a memory server is a bad trade.

Registrations point at the **pinned service venv**, not the workspace
one. The workspace venv is an editable install anchored to a worktree,
and when that anchor broke in July every axiom entry point died with it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PINNED = "/Users/x/.axi/service-venv/bin/python"


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


def test_cursor_registers_and_preserves_other_servers(tmp_path: Path):
    from axiom.extensions.builtins.memory.register_mcp import register_cursor_mcp

    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"other": {"command": "/bin/other", "args": []}}
    }))

    result = register_cursor_mcp(config_path=cfg, python_path=PINNED)

    data = json.loads(cfg.read_text())
    assert result["action"] == "added"
    assert data["mcpServers"]["other"]["command"] == "/bin/other"
    entry = data["mcpServers"]["axiom-memory"]
    assert entry["command"] == PINNED
    assert entry["args"] == ["-m", "axiom.extensions.builtins.memory.mcp_server"]


def test_cursor_registration_is_idempotent(tmp_path: Path):
    from axiom.extensions.builtins.memory.register_mcp import register_cursor_mcp

    cfg = tmp_path / "mcp.json"
    cfg.write_text("{}")

    assert register_cursor_mcp(config_path=cfg, python_path=PINNED)["action"] == "added"
    second = register_cursor_mcp(config_path=cfg, python_path=PINNED)
    assert second["action"] == "unchanged"


def test_cursor_repoints_a_stale_command(tmp_path: Path):
    """The July failure: entries pinned to the fragile editable venv."""
    from axiom.extensions.builtins.memory.register_mcp import register_cursor_mcp

    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"axiom-memory": {
        "command": "/Users/x/Projects/ws/.venv/bin/python3.14",
        "args": ["-m", "axiom.extensions.builtins.memory.mcp_server"],
    }}}))

    result = register_cursor_mcp(config_path=cfg, python_path=PINNED)

    assert result["action"] == "updated"
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["axiom-memory"]["command"] == PINNED


# ---------------------------------------------------------------------------
# VS Code — different key, and entries carry a type
# ---------------------------------------------------------------------------


def test_vscode_uses_servers_key_with_stdio_type(tmp_path: Path):
    from axiom.extensions.builtins.memory.register_mcp import register_vscode_mcp

    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({
        "servers": {"stripe": {"type": "http", "url": "https://mcp.stripe.com"}},
        "inputs": [{"id": "Authorization", "type": "promptString"}],
    }))

    register_vscode_mcp(config_path=cfg, python_path=PINNED)

    data = json.loads(cfg.read_text())
    entry = data["servers"]["axiom-memory"]
    assert entry["type"] == "stdio"
    assert entry["command"] == PINNED
    # Untouched neighbours, including VS Code's own inputs plumbing.
    assert data["servers"]["stripe"]["url"] == "https://mcp.stripe.com"
    assert data["inputs"][0]["id"] == "Authorization"


def test_vscode_is_idempotent(tmp_path: Path):
    from axiom.extensions.builtins.memory.register_mcp import register_vscode_mcp

    cfg = tmp_path / "mcp.json"
    cfg.write_text("{}")
    assert register_vscode_mcp(config_path=cfg, python_path=PINNED)["action"] == "added"
    assert register_vscode_mcp(
        config_path=cfg, python_path=PINNED,
    )["action"] == "unchanged"


# ---------------------------------------------------------------------------
# Claude desktop
# ---------------------------------------------------------------------------


def test_claude_desktop_preserves_unrelated_settings(tmp_path: Path):
    """That file holds preferences too, not just servers."""
    from axiom.extensions.builtins.memory.register_mcp import (
        register_claude_desktop_mcp,
    )

    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({
        "coworkUserFilesPath": "/Users/x/files",
        "preferences": {"theme": "dark"},
    }))

    register_claude_desktop_mcp(config_path=cfg, python_path=PINNED)

    data = json.loads(cfg.read_text())
    assert data["coworkUserFilesPath"] == "/Users/x/files"
    assert data["preferences"] == {"theme": "dark"}
    assert data["mcpServers"]["axiom-memory"]["command"] == PINNED


# ---------------------------------------------------------------------------
# Shared safety rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("registrar_name", [
    "register_cursor_mcp", "register_vscode_mcp", "register_claude_desktop_mcp",
])
def test_registrar_refuses_to_rewrite_malformed_config(
    registrar_name: str, tmp_path: Path,
):
    """A harness config is the user's; never clobber what we can't parse."""
    import axiom.extensions.builtins.memory.register_mcp as reg

    cfg = tmp_path / "cfg.json"
    cfg.write_text("{ not json at all")

    with pytest.raises(ValueError):
        getattr(reg, registrar_name)(config_path=cfg, python_path=PINNED)

    assert cfg.read_text() == "{ not json at all"


@pytest.mark.parametrize("tool", ["cursor", "vscode", "claude-desktop"])
def test_registrars_are_in_the_registry(tool: str):
    from axiom.extensions.builtins.memory.register_mcp import TOOL_REGISTRARS

    assert tool in TOOL_REGISTRARS
    assert TOOL_REGISTRARS[tool].detect is not None


def test_pinned_python_is_preferred_over_the_editable_venv(tmp_path: Path):
    """Default target must be the churn-immune install.

    The workspace venv is an editable install anchored to a worktree;
    when that anchor moved in July, every axiom entry point broke and
    capture died silently for three weeks.
    """
    from axiom.extensions.builtins.memory.register_mcp import _pinned_python

    service_venv = tmp_path / ".axi" / "service-venv" / "bin"
    service_venv.mkdir(parents=True)
    python = service_venv / "python"
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)

    assert _pinned_python(home=tmp_path) == str(python)


def test_pinned_python_falls_back_when_absent(tmp_path: Path):
    import sys

    from axiom.extensions.builtins.memory.register_mcp import _pinned_python

    assert _pinned_python(home=tmp_path) == sys.executable
