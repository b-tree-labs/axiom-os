# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Registrars for the agent harnesses — Hermes (file) and OpenClaw (CLI).

Hermes keeps its MCP servers in ``~/.hermes/config.yaml`` under
``mcp_servers`` — a format verified from a live install and from the
harness's own client docstring — so its registrar goes through the shared
matrix's YAML writer like every other file-backed harness. It used to
shell out to ``hermes mcp add``; that failed on any machine where
``~/.hermes`` exists but the ``hermes`` binary is not on PATH (detection
said yes, registration said ``not_installed``), and it could not preserve
the ``tools.include`` allowlist the writer now keeps.

OpenClaw remains the one CLI-driven registrar. Its config nests MCP
servers differently depending on which doc you read (``mcp.servers`` vs
``mcpServers``), and guessing wrong would either silently fail or corrupt
a config the user owns. Letting the tool write its own config removes the
guess entirely — and is why OpenClaw is the documented ``REGISTRAR_ONLY``
exception to the matrix parity rule rather than a ``TOOL_SPECS`` row.

Note on naming: **Clawdbot no longer exists.** It became Moltbot on
2026-01-27 and OpenClaw on 2026-01-30 after a trademark dispute, and
attackers hijacked the abandoned names and domains during the churn.
The registrar is deliberately keyed ``openclaw`` and detects only the
``openclaw`` binary — nothing here should ever reach for a "clawdbot"
install path.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest


def _fake_binary(tmp_path: Path, name: str, script: str) -> Path:
    """A stand-in CLI on PATH, so registrars can be tested without installs."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    path = bindir / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


PINNED = "/Users/x/.axi/service-venv/bin/python"
MEMORY_ARGS = ["-m", "axiom.extensions.builtins.memory.mcp_server"]


def _hermes_home(tmp_path: Path, monkeypatch) -> Path:
    """An isolated home with no ``hermes`` binary; returns the config path
    the spec resolves for it."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("AXIOM_HERMES_CONFIG", raising=False)
    cfg = tmp_path / ".hermes" / "config.yaml"
    cfg.parent.mkdir(exist_ok=True)
    return cfg


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_hermes_not_detected_when_absent(tmp_path, monkeypatch):
    from axiom.extensions.builtins.memory.register_mcp import _detect_hermes

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert _detect_hermes() is False


def test_hermes_detected_via_binary(tmp_path, monkeypatch):
    from axiom.extensions.builtins.memory.register_mcp import _detect_hermes

    _fake_binary(tmp_path, "hermes", "exit 0\n")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert _detect_hermes() is True


def test_hermes_detected_via_store_dir_without_binary(tmp_path, monkeypatch):
    """The common case on a dev box: ``~/.hermes`` exists, no CLI on PATH.
    Detection and registration must agree here — both file-based."""
    from axiom.extensions.builtins.memory.register_mcp import _detect_hermes

    _hermes_home(tmp_path, monkeypatch)
    assert _detect_hermes() is True


def test_openclaw_detected_via_config_dir(tmp_path, monkeypatch):
    from axiom.extensions.builtins.memory.register_mcp import _detect_openclaw

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    (tmp_path / ".openclaw").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert _detect_openclaw() is True


def test_clawdbot_is_not_a_detectable_tool():
    """The old name was hijacked during the rebrand; never reach for it."""
    from axiom.extensions.builtins.memory.register_mcp import TOOL_REGISTRARS

    assert "clawdbot" not in TOOL_REGISTRARS
    assert "moltbot" not in TOOL_REGISTRARS
    assert "openclaw" in TOOL_REGISTRARS


# ---------------------------------------------------------------------------
# Hermes: registration writes config.yaml through the shared matrix
# ---------------------------------------------------------------------------


def test_hermes_register_writes_config_yaml(tmp_path, monkeypatch):
    yaml = pytest.importorskip("yaml")
    from axiom.extensions.builtins.memory.register_mcp import (
        is_hermes_mcp_registered,
        register_hermes_mcp,
    )

    cfg = _hermes_home(tmp_path, monkeypatch)
    cfg.write_text(
        yaml.safe_dump(
            {
                "_config_version": 37,
                "mcp_servers": {
                    "filesystem": {"command": "npx", "args": ["-y", "server-filesystem"]},
                },
            }
        )
    )

    result = register_hermes_mcp(python_path=PINNED)

    assert result["action"] == "added"
    assert result["tool"] == "hermes"
    assert result["config_path"] == str(cfg)
    data = yaml.safe_load(cfg.read_text())
    entry = data["mcp_servers"]["axiom-memory"]
    assert entry["command"] == PINNED
    assert entry["args"] == MEMORY_ARGS
    assert entry["enabled"] is True
    # Neighbours and unrelated top-level config survive.
    assert data["mcp_servers"]["filesystem"]["command"] == "npx"
    assert data["_config_version"] == 37
    assert is_hermes_mcp_registered()["registered"] is True


def test_hermes_reregister_keeps_tools_include_and_is_idempotent(tmp_path, monkeypatch):
    """``tools.include`` is the allowlist keeping a third-party agent away
    from the credential tools; repointing the interpreter must not drop it."""
    yaml = pytest.importorskip("yaml")
    from axiom.extensions.builtins.memory.register_mcp import register_hermes_mcp

    cfg = _hermes_home(tmp_path, monkeypatch)
    cfg.write_text(
        yaml.safe_dump(
            {
                "mcp_servers": {
                    "axiom-memory": {
                        "command": "/Users/x/Projects/ws/.venv/bin/python3.14",
                        "args": MEMORY_ARGS,
                        "enabled": True,
                        "tools": {"include": ["axiom_memory_recall", "axiom_memory_append"]},
                    }
                }
            }
        )
    )

    assert register_hermes_mcp(python_path=PINNED)["action"] == "updated"
    entry = yaml.safe_load(cfg.read_text())["mcp_servers"]["axiom-memory"]
    assert entry["command"] == PINNED
    assert entry["tools"]["include"] == ["axiom_memory_recall", "axiom_memory_append"]

    assert register_hermes_mcp(python_path=PINNED)["action"] == "unchanged"
    assert list(yaml.safe_load(cfg.read_text())["mcp_servers"]) == ["axiom-memory"]


def test_hermes_register_creates_config_when_missing(tmp_path, monkeypatch):
    yaml = pytest.importorskip("yaml")
    from axiom.extensions.builtins.memory.register_mcp import register_hermes_mcp

    cfg = _hermes_home(tmp_path, monkeypatch)
    assert not cfg.exists()

    assert register_hermes_mcp(python_path=PINNED)["action"] == "added"
    assert yaml.safe_load(cfg.read_text())["mcp_servers"]["axiom-memory"]["command"] == PINNED


def test_hermes_register_refuses_to_rewrite_malformed_yaml(tmp_path, monkeypatch):
    from axiom.extensions.builtins.memory.register_mcp import (
        is_hermes_mcp_registered,
        register_hermes_mcp,
    )

    cfg = _hermes_home(tmp_path, monkeypatch)
    cfg.write_text("{ [ } ]\n")

    with pytest.raises(ValueError):
        register_hermes_mcp(python_path=PINNED)
    assert cfg.read_text() == "{ [ } ]\n"

    status = is_hermes_mcp_registered()
    assert status["registered"] is False
    assert status["reason"] == "malformed_config"


# ---------------------------------------------------------------------------
# Hermes: is_registered reads back the same file
# ---------------------------------------------------------------------------


def test_hermes_is_registered_true_when_present(tmp_path, monkeypatch):
    yaml = pytest.importorskip("yaml")
    from axiom.extensions.builtins.memory.register_mcp import is_hermes_mcp_registered

    cfg = _hermes_home(tmp_path, monkeypatch)
    cfg.write_text(
        yaml.safe_dump(
            {
                "mcp_servers": {
                    "axiom-memory": {"command": PINNED, "args": MEMORY_ARGS, "enabled": True},
                }
            }
        )
    )

    status = is_hermes_mcp_registered(expected_command=PINNED)
    assert status["registered"] is True
    assert status["command"] == PINNED
    assert status["stale"] is False
    assert is_hermes_mcp_registered(expected_command="/new/python")["stale"] is True


def test_hermes_is_registered_false_when_absent(tmp_path, monkeypatch):
    yaml = pytest.importorskip("yaml")
    from axiom.extensions.builtins.memory.register_mcp import is_hermes_mcp_registered

    cfg = _hermes_home(tmp_path, monkeypatch)
    assert is_hermes_mcp_registered()["reason"] == "missing"  # no file

    cfg.write_text(yaml.safe_dump({"mcp_servers": {"some-other-server": {"command": "x"}}}))
    status = is_hermes_mcp_registered()
    assert status["registered"] is False
    assert status["reason"] == "missing"


# ---------------------------------------------------------------------------
# OpenClaw: registration drives the tool's own CLI
# ---------------------------------------------------------------------------


def test_openclaw_register_invokes_mcp_add(tmp_path, monkeypatch):
    from axiom.extensions.builtins.memory.register_mcp import register_openclaw_mcp

    log = tmp_path / "invocation.log"
    _fake_binary(tmp_path, "openclaw", f'echo "$@" >> {log}\nexit 0\n')
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    result = register_openclaw_mcp(python_path=PINNED)

    assert result["action"] == "added"
    invocation = log.read_text()
    assert "mcp add" in invocation
    assert "axiom-memory" in invocation
    assert PINNED in invocation
    # --args must come last: it swallows the remaining argv.
    assert invocation.strip().endswith("-m axiom.extensions.builtins.memory.mcp_server")


def test_register_reports_failure_instead_of_raising(tmp_path, monkeypatch):
    """A failing CLI is a reported outcome, not an exception mid-sweep."""
    from axiom.extensions.builtins.memory.register_mcp import register_openclaw_mcp

    _fake_binary(tmp_path, "openclaw", 'echo "boom" >&2\nexit 3\n')
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    result = register_openclaw_mcp(python_path=PINNED)
    assert result["action"] == "failed"
    assert "boom" in result.get("error", "")


def test_register_when_binary_missing_is_reported(tmp_path, monkeypatch):
    from axiom.extensions.builtins.memory.register_mcp import register_openclaw_mcp

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    result = register_openclaw_mcp(python_path=PINNED)
    assert result["action"] == "failed"
    assert "not_installed" in result.get("reason", "")


def test_openclaw_is_registered_reads_mcp_list(tmp_path, monkeypatch):
    from axiom.extensions.builtins.memory.register_mcp import is_openclaw_mcp_registered

    _fake_binary(tmp_path, "openclaw", 'echo "axiom-memory  stdio  enabled"\nexit 0\n')
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert is_openclaw_mcp_registered()["registered"] is True

    _fake_binary(tmp_path, "openclaw", 'echo "some-other-server"\nexit 0\n')
    assert is_openclaw_mcp_registered()["registered"] is False


# ---------------------------------------------------------------------------
# Registry membership + mechanism
# ---------------------------------------------------------------------------


def test_registrars_are_in_the_registry():
    from axiom.extensions.builtins.memory.register_mcp import (
        REGISTRAR_ONLY,
        TOOL_REGISTRARS,
    )

    for tool in ("hermes", "openclaw"):
        assert tool in TOOL_REGISTRARS
        assert TOOL_REGISTRARS[tool].detect is not None

    assert TOOL_REGISTRARS["hermes"].mechanism == "file"
    assert TOOL_REGISTRARS["hermes"].spec is not None
    assert TOOL_REGISTRARS["openclaw"].mechanism == "cli"
    assert TOOL_REGISTRARS["openclaw"].spec is None
    assert frozenset({"openclaw"}) == REGISTRAR_ONLY
