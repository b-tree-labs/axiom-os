# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the axiom-memory MCP registration check in `axi dr`."""

from __future__ import annotations

import sys
from pathlib import Path


def test_check_axiom_memory_mcp_returns_error_when_not_registered(
    tmp_path: Path, monkeypatch,
):
    from axiom.cli.doctor import (
        CheckStatus,
        check_axiom_memory_mcp_registered,
    )

    config = tmp_path / "claude.json"
    config.write_text("{}")
    monkeypatch.setenv("AXIOM_CLAUDE_CONFIG", str(config))

    result = check_axiom_memory_mcp_registered()
    assert result.status == CheckStatus.ERROR
    assert "axi memory register-mcp" in (result.fix_hint or "")


def test_check_axiom_memory_mcp_returns_ok_when_registered_with_current_python(
    tmp_path: Path, monkeypatch,
):
    from axiom.cli.doctor import (
        CheckStatus,
        check_axiom_memory_mcp_registered,
    )
    from axiom.extensions.builtins.memory.register_mcp import (
        register_axiom_memory_mcp,
    )

    config = tmp_path / "claude.json"
    monkeypatch.setenv("AXIOM_CLAUDE_CONFIG", str(config))
    # No pinned service venv under this home, so the interpreter the
    # registrar resolves *is* the current python.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    register_axiom_memory_mcp(python_path=sys.executable)

    result = check_axiom_memory_mcp_registered()
    assert result.status == CheckStatus.OK


def test_check_axiom_memory_mcp_ok_when_registered_with_pinned_interpreter(
    tmp_path: Path, monkeypatch,
):
    """The registrar pins the service venv when one exists; doctor judges
    staleness against that same resolution, not the python it happens to
    run under — otherwise every deliberately pinned registration would be
    nagged as stale by a doctor run from the workspace venv."""
    from axiom.cli.doctor import (
        CheckStatus,
        check_axiom_memory_mcp_registered,
    )
    from axiom.extensions.builtins.memory.register_mcp import (
        register_axiom_memory_mcp,
        resolve_memory_server,
    )

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    pinned = tmp_path / ".axi" / "service-venv" / "bin" / "python"
    pinned.parent.mkdir(parents=True)
    pinned.write_text("#!/bin/sh\n")
    pinned.chmod(0o755)
    config = tmp_path / "claude.json"
    monkeypatch.setenv("AXIOM_CLAUDE_CONFIG", str(config))
    assert resolve_memory_server()[1] == str(pinned)
    assert str(pinned) != sys.executable

    register_axiom_memory_mcp()  # default resolution -> the pinned interpreter

    result = check_axiom_memory_mcp_registered()
    assert result.status == CheckStatus.OK, result.summary


def test_check_axiom_memory_mcp_returns_warning_when_stale_python(
    tmp_path: Path, monkeypatch,
):
    from axiom.cli.doctor import (
        CheckStatus,
        check_axiom_memory_mcp_registered,
    )
    from axiom.extensions.builtins.memory.register_mcp import (
        register_axiom_memory_mcp,
    )

    config = tmp_path / "claude.json"
    monkeypatch.setenv("AXIOM_CLAUDE_CONFIG", str(config))

    # Register with a python path that is intentionally not sys.executable.
    register_axiom_memory_mcp(python_path="/path/that/does/not/match")

    result = check_axiom_memory_mcp_registered()
    assert result.status == CheckStatus.WARNING
    assert "stale" in (result.summary or "").lower()
    assert "axi memory register-mcp" in (result.fix_hint or "")


def test_doctor_default_checks_include_axiom_memory_mcp_check():
    from axiom.cli.doctor import default_checks

    names = [c.name for c in default_checks()]
    assert any("axiom-memory MCP" in n for n in names)
