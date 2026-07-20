# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""doctor check: unified MCP server registered in an IDE."""

from __future__ import annotations

from axiom.cli.doctor import CheckStatus, check_unified_mcp_installed
from axiom.extensions.builtins.mcp import install as inst

_ENV_KEYS = (
    "AXIOM_CLAUDE_CONFIG",
    "AXIOM_CLAUDE_DESKTOP_CONFIG",
    "AXIOM_CURSOR_CONFIG",
    "AXIOM_WINDSURF_CONFIG",
    "AXIOM_GEMINI_CONFIG",
    "AXIOM_OPENCODE_CONFIG",
    "AXIOM_VSCODE_CONFIG",
    "AXIOM_CODEX_CONFIG",
)


def _isolate(tmp_path, monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.setenv(k, str(tmp_path / f"{k}.cfg"))


def test_ok_when_registered(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    inst.install(tools=["cursor"])
    r = check_unified_mcp_installed()
    assert r.status == CheckStatus.OK
    assert "cursor" in r.summary


def test_warns_when_detected_but_unregistered(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)  # all configs empty → nothing registered
    monkeypatch.setattr(inst, "detect_tools", lambda: {"cursor": True})
    r = check_unified_mcp_installed()
    assert r.status == CheckStatus.WARNING
    assert r.fix_hint == "axi mcp install"


def test_ok_when_no_ide_detected(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(inst, "detect_tools", lambda: dict.fromkeys(inst.supported_tools(), False))
    r = check_unified_mcp_installed()
    assert r.status == CheckStatus.OK
    assert "no MCP-capable IDEs" in r.summary
