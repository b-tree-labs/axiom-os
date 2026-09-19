# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the per-tool MCP registrar protocol.

Each LLM tool (Claude Code, Codex, Gemini, OpenCode, …) writes to its own
config file in its own format. Rather than hand-write per-tool branches
in `axi memory register-mcp`, we expose a registry where each tool's
registrar declares: detect (is the tool installed?), register (write
the MCP entry idempotently), and is_registered (read-only check).

The CLI's `--all` flag walks this registry, calling each registrar that
also reports detected. Every registrar is a thin adapter over the one
matrix in ``mcp/install.py`` (``TOOL_SPECS``); there are no stubs — a
harness the matrix knows registers for real, and adding a harness is one
matrix row (see ``test_registrar_matrix.py`` for the parity guarantee).
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Registry membership
# ---------------------------------------------------------------------------


def test_registrar_registry_includes_known_tools():
    from axiom.extensions.builtins.memory.register_mcp import TOOL_REGISTRARS

    names = set(TOOL_REGISTRARS)
    assert "claude-code" in names
    assert "codex" in names
    assert "gemini" in names
    assert "opencode" in names


def test_each_registrar_declares_detect_register_is_registered():
    from axiom.extensions.builtins.memory.register_mcp import TOOL_REGISTRARS

    for name, reg in TOOL_REGISTRARS.items():
        assert callable(reg.detect), f"{name}.detect not callable"
        assert callable(reg.register), f"{name}.register not callable"
        assert callable(reg.is_registered), f"{name}.is_registered not callable"


# ---------------------------------------------------------------------------
# Codex registrar (direct TOML edit; idempotent)
# ---------------------------------------------------------------------------


def test_codex_register_writes_entry_to_empty_config(tmp_path: Path):
    from axiom.extensions.builtins.memory.register_mcp import register_codex_mcp

    config_path = tmp_path / "codex.toml"
    config_path.write_text("")

    result = register_codex_mcp(
        config_path=config_path,
        python_path="/path/to/python",
    )
    assert result["action"] == "added"

    # Read back via tomllib
    import tomllib
    data = tomllib.loads(config_path.read_text())
    entry = data["mcp_servers"]["axiom-memory"]
    assert entry["command"] == "/path/to/python"
    assert entry["args"] == [
        "-m", "axiom.extensions.builtins.memory.mcp_server",
    ]


def test_codex_register_idempotent(tmp_path: Path):
    from axiom.extensions.builtins.memory.register_mcp import register_codex_mcp

    config_path = tmp_path / "codex.toml"
    register_codex_mcp(
        config_path=config_path, python_path="/path/to/python",
    )

    result = register_codex_mcp(
        config_path=config_path, python_path="/path/to/python",
    )
    assert result["action"] == "unchanged"


def test_codex_register_updates_when_python_path_differs(tmp_path: Path):
    from axiom.extensions.builtins.memory.register_mcp import register_codex_mcp

    config_path = tmp_path / "codex.toml"
    register_codex_mcp(config_path=config_path, python_path="/old/python")

    result = register_codex_mcp(
        config_path=config_path, python_path="/new/python",
    )
    assert result["action"] == "updated"


def test_codex_register_preserves_other_mcp_servers(tmp_path: Path):
    from axiom.extensions.builtins.memory.register_mcp import register_codex_mcp

    config_path = tmp_path / "codex.toml"
    config_path.write_text("""
[model_providers.openai]
name = "OpenAI"

[mcp_servers.other-server]
command = "other"
args = ["x"]
""")

    register_codex_mcp(
        config_path=config_path, python_path="/path/to/python",
    )

    import tomllib
    data = tomllib.loads(config_path.read_text())
    assert "axiom-memory" in data["mcp_servers"]
    assert "other-server" in data["mcp_servers"]
    assert data["model_providers"]["openai"]["name"] == "OpenAI"


def test_codex_is_registered_detects_present_and_missing(tmp_path: Path):
    from axiom.extensions.builtins.memory.register_mcp import (
        is_codex_mcp_registered,
        register_codex_mcp,
    )

    config_path = tmp_path / "codex.toml"
    config_path.write_text("")

    status = is_codex_mcp_registered(config_path=config_path)
    assert status["registered"] is False

    register_codex_mcp(config_path=config_path, python_path="/p/python")

    status = is_codex_mcp_registered(config_path=config_path)
    assert status["registered"] is True


# ---------------------------------------------------------------------------
# Gemini + OpenCode — real registrars through the shared matrix (no stubs)
# ---------------------------------------------------------------------------


def test_gemini_register_writes_mcp_servers_in_settings(tmp_path: Path):
    """Gemini CLI reads ``~/.gemini/settings.json`` -> ``mcpServers``; the
    rest of that settings file is the user's and must survive."""
    from axiom.extensions.builtins.memory.register_mcp import (
        TOOL_REGISTRARS,
        is_gemini_mcp_registered,
    )

    cfg = tmp_path / "settings.json"
    cfg.write_text(json.dumps({
        "theme": "Default",
        "mcpServers": {"other": {"command": "x"}},
    }))

    result = TOOL_REGISTRARS["gemini"].register(config_path=cfg, python_path="/p/python")

    assert result["action"] == "added"
    data = json.loads(cfg.read_text())
    entry = data["mcpServers"]["axiom-memory"]
    assert entry["command"] == "/p/python"
    assert entry["args"] == ["-m", "axiom.extensions.builtins.memory.mcp_server"]
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert data["theme"] == "Default"
    assert is_gemini_mcp_registered(config_path=cfg)["registered"] is True


def test_opencode_register_writes_local_mcp_entry(tmp_path: Path):
    """OpenCode keys servers under ``mcp`` with ``type: local`` and the whole
    argv as ``command`` (schema: https://opencode.ai/config.json) — not the
    ``mcpServers`` shape the other JSON clients share."""
    from axiom.extensions.builtins.memory.register_mcp import (
        TOOL_REGISTRARS,
        is_opencode_mcp_registered,
    )

    cfg = tmp_path / "opencode.jsonc"
    cfg.write_text(json.dumps({"$schema": "https://opencode.ai/config.json"}))

    result = TOOL_REGISTRARS["opencode"].register(config_path=cfg, python_path="/p/python")

    assert result["action"] == "added"
    data = json.loads(cfg.read_text())
    entry = data["mcp"]["axiom-memory"]
    assert entry["type"] == "local"
    assert entry["command"] == [
        "/p/python", "-m", "axiom.extensions.builtins.memory.mcp_server",
    ]
    assert entry["enabled"] is True
    assert "mcpServers" not in data
    assert data["$schema"] == "https://opencode.ai/config.json"
    status = is_opencode_mcp_registered(config_path=cfg)
    assert status["registered"] is True
    assert status["command"] == "/p/python"


def test_no_registrar_is_a_stub():
    """Every tool the matrix knows registers for real. The contributor
    pointer that used to live in a NotImplementedError is now the matrix
    row itself."""
    from axiom.extensions.builtins.memory.register_mcp import (
        REGISTRAR_ONLY,
        TOOL_REGISTRARS,
    )

    for name, reg in TOOL_REGISTRARS.items():
        if name in REGISTRAR_ONLY:
            continue
        assert reg.spec is not None, name
        assert reg.mechanism == "file", name


# ---------------------------------------------------------------------------
# Detection — should not raise even when binary is missing
# ---------------------------------------------------------------------------


def test_detect_installed_tools_runs_without_error():
    from axiom.extensions.builtins.memory.register_mcp import detect_installed_tools

    result = detect_installed_tools()
    assert isinstance(result, dict)
    # At least claude-code (file-based) and codex (binary check) report a bool
    for name in ("claude-code", "codex", "gemini", "opencode"):
        assert name in result
        assert isinstance(result[name], bool)


# ---------------------------------------------------------------------------
# CLI: axi memory register-mcp --all + --tool
# ---------------------------------------------------------------------------


def test_cli_register_mcp_all_dispatches_to_detected(
    tmp_path, monkeypatch, capsys,
):
    """`--all` only registers tools that detect() returns True for."""
    from axiom.extensions.builtins.memory import cli, register_mcp

    monkeypatch.setattr(
        register_mcp, "detect_installed_tools",
        lambda: {"claude-code": True, "codex": False, "gemini": False, "opencode": False},
    )

    # Stub claude-code path so we don't write to ~/.claude.json
    claude_config = tmp_path / "claude.json"
    monkeypatch.setenv("AXIOM_CLAUDE_CONFIG", str(claude_config))

    rc = cli.main(["register-mcp", "--all", "--json"])
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    # Result is a per-tool dict; only claude-code should appear (codex was skipped).
    assert "claude-code" in payload
    assert payload["claude-code"]["action"] in ("added", "unchanged")
    # Codex was not detected → skipped (not present in the report, or marked skipped).
    assert "codex" not in payload or payload["codex"].get("action") == "skipped"


def test_cli_register_mcp_unknown_tool_returns_error(tmp_path, monkeypatch, capsys):
    from axiom.extensions.builtins.memory import cli

    rc = cli.main([
        "register-mcp",
        "--tool", "not-a-real-tool",
    ])
    assert rc != 0
    err = capsys.readouterr().err.lower()
    assert "tool" in err
