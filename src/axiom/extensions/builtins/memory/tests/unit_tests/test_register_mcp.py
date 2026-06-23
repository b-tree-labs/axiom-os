# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom-memory MCP user-scope registration + idempotency.

`axi memory register-mcp` writes the axiom-memory entry to ~/.claude.json
(or a configurable config path) so every Claude Code session on this
machine reaches the MCP. Idempotent: re-running with the same python
path is a no-op; re-running with a different python path updates in
place.

`axi dr` consumes `is_registered()` to detect missing or stale entries
and surfaces a fix_hint.
"""

from __future__ import annotations

import json
from pathlib import Path



# ---------------------------------------------------------------------------
# register_axiom_memory_mcp — pure function over a config file
# ---------------------------------------------------------------------------


def test_register_writes_entry_to_empty_config(tmp_path: Path):
    from axiom.extensions.builtins.memory.register_mcp import (
        register_axiom_memory_mcp,
    )

    config_path = tmp_path / "claude.json"
    config_path.write_text(json.dumps({}))

    result = register_axiom_memory_mcp(
        config_path=config_path,
        python_path="/path/to/python",
    )
    assert result["action"] == "added"
    assert result["command"] == "/path/to/python"

    data = json.loads(config_path.read_text())
    entry = data["mcpServers"]["axiom-memory"]
    assert entry["command"] == "/path/to/python"
    assert entry["args"] == [
        "-m", "axiom.extensions.builtins.memory.mcp_server",
    ]
    assert entry["type"] == "stdio"


def test_register_creates_config_file_if_missing(tmp_path: Path):
    from axiom.extensions.builtins.memory.register_mcp import (
        register_axiom_memory_mcp,
    )

    config_path = tmp_path / "nonexistent.json"

    result = register_axiom_memory_mcp(
        config_path=config_path,
        python_path="/path/to/python",
    )
    assert result["action"] == "added"
    assert config_path.exists()


def test_register_idempotent_when_already_present(tmp_path: Path):
    from axiom.extensions.builtins.memory.register_mcp import (
        register_axiom_memory_mcp,
    )

    config_path = tmp_path / "claude.json"
    register_axiom_memory_mcp(
        config_path=config_path, python_path="/path/to/python",
    )

    result = register_axiom_memory_mcp(
        config_path=config_path, python_path="/path/to/python",
    )
    assert result["action"] == "unchanged"


def test_register_updates_when_python_path_differs(tmp_path: Path):
    from axiom.extensions.builtins.memory.register_mcp import (
        register_axiom_memory_mcp,
    )

    config_path = tmp_path / "claude.json"
    register_axiom_memory_mcp(
        config_path=config_path, python_path="/old/python",
    )

    result = register_axiom_memory_mcp(
        config_path=config_path, python_path="/new/python",
    )
    assert result["action"] == "updated"

    data = json.loads(config_path.read_text())
    assert data["mcpServers"]["axiom-memory"]["command"] == "/new/python"


def test_register_preserves_other_mcp_servers(tmp_path: Path):
    """Registering axiom-memory must not clobber other registered servers."""
    from axiom.extensions.builtins.memory.register_mcp import (
        register_axiom_memory_mcp,
    )

    config_path = tmp_path / "claude.json"
    config_path.write_text(json.dumps({
        "mcpServers": {
            "linear": {"type": "http", "url": "https://x"},
            "axiom-classroom": {"type": "stdio", "command": "py"},
        },
        "otherKey": "preserved",
    }))

    register_axiom_memory_mcp(
        config_path=config_path, python_path="/path/to/python",
    )

    data = json.loads(config_path.read_text())
    assert "linear" in data["mcpServers"]
    assert "axiom-classroom" in data["mcpServers"]
    assert "axiom-memory" in data["mcpServers"]
    assert data["otherKey"] == "preserved"


# ---------------------------------------------------------------------------
# is_axiom_memory_mcp_registered — detection
# ---------------------------------------------------------------------------


def test_is_registered_false_when_missing(tmp_path: Path):
    from axiom.extensions.builtins.memory.register_mcp import (
        is_axiom_memory_mcp_registered,
    )

    config_path = tmp_path / "claude.json"
    config_path.write_text(json.dumps({"mcpServers": {}}))

    status = is_axiom_memory_mcp_registered(config_path=config_path)
    assert status["registered"] is False
    assert status["reason"] == "missing"


def test_is_registered_true_when_present(tmp_path: Path):
    from axiom.extensions.builtins.memory.register_mcp import (
        is_axiom_memory_mcp_registered,
        register_axiom_memory_mcp,
    )

    config_path = tmp_path / "claude.json"
    register_axiom_memory_mcp(
        config_path=config_path, python_path="/path/to/python",
    )

    status = is_axiom_memory_mcp_registered(config_path=config_path)
    assert status["registered"] is True
    assert status["command"] == "/path/to/python"


def test_is_registered_flags_stale_python_path(tmp_path: Path):
    """Detects when registered command differs from expected python."""
    from axiom.extensions.builtins.memory.register_mcp import (
        is_axiom_memory_mcp_registered,
        register_axiom_memory_mcp,
    )

    config_path = tmp_path / "claude.json"
    register_axiom_memory_mcp(
        config_path=config_path, python_path="/old/python",
    )

    status = is_axiom_memory_mcp_registered(
        config_path=config_path, expected_command="/new/python",
    )
    assert status["registered"] is True
    assert status["stale"] is True


# ---------------------------------------------------------------------------
# CLI: axi memory register-mcp
# ---------------------------------------------------------------------------


def test_cli_register_mcp_writes_with_sys_executable(
    tmp_path: Path, monkeypatch, capsys,
):
    from axiom.extensions.builtins.memory import cli

    config_path = tmp_path / "claude.json"
    monkeypatch.setenv("AXIOM_CLAUDE_CONFIG", str(config_path))

    rc = cli.main(["register-mcp", "--json"])
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] in ("added", "unchanged")
    assert payload["command"]  # sys.executable

    data = json.loads(config_path.read_text())
    assert "axiom-memory" in data["mcpServers"]


def test_cli_register_mcp_check_only_returns_nonzero_when_missing(
    tmp_path: Path, monkeypatch, capsys,
):
    from axiom.extensions.builtins.memory import cli

    config_path = tmp_path / "claude.json"
    config_path.write_text("{}")
    monkeypatch.setenv("AXIOM_CLAUDE_CONFIG", str(config_path))

    rc = cli.main(["register-mcp", "--check"])
    assert rc != 0


def test_cli_register_mcp_check_returns_zero_when_present(
    tmp_path: Path, monkeypatch, capsys,
):
    from axiom.extensions.builtins.memory import cli

    config_path = tmp_path / "claude.json"
    monkeypatch.setenv("AXIOM_CLAUDE_CONFIG", str(config_path))

    cli.main(["register-mcp"])
    capsys.readouterr()

    rc = cli.main(["register-mcp", "--check"])
    assert rc == 0
