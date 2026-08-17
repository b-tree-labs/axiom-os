# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the per-tool parser dispatch in `axi memory ingest --tool`.

The dispatcher routes a session-log path to the right parser based on
``--tool``. Claude Code is the canonical implementation; other tools
(OpenCode, Gemini, ChatGPT-Desktop) are stubs that surface a clear
"contribute a parser at <path>" error so they can be added incrementally
without changing the dispatch surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Parser registry — known tools + claude-code as canonical
# ---------------------------------------------------------------------------


def test_registry_lists_known_tool_names():
    from axiom.memory.session_capture import KNOWN_TOOL_PARSERS

    assert "claude-code" in KNOWN_TOOL_PARSERS
    # Stubs for known-but-unimplemented surface tools.
    assert "opencode" in KNOWN_TOOL_PARSERS
    assert "gemini" in KNOWN_TOOL_PARSERS
    assert "chatgpt-desktop" in KNOWN_TOOL_PARSERS


# ---------------------------------------------------------------------------
# Dispatch: ingest_session_log routes by tool name
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_composition(tmp_path: Path):
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


def test_dispatch_claude_code_works(isolated_composition, tmp_path):
    from axiom.memory.session_capture import ingest_session_log

    transcript = tmp_path / "session.jsonl"
    with transcript.open("w") as f:
        f.write(json.dumps({
            "type": "user", "uuid": "u-1", "timestamp": "2026-05-01T10:00:00Z",
            "sessionId": "s-1",
            "message": {"role": "user", "content": "hi"},
        }) + "\n")
        f.write(json.dumps({
            "type": "assistant", "uuid": "a-1", "timestamp": "2026-05-01T10:00:05Z",
            "sessionId": "s-1",
            "message": {"role": "assistant", "model": "x",
                        "content": [{"type": "text", "text": "hello"}]},
        }) + "\n")

    report = ingest_session_log(
        composition=isolated_composition,
        path=str(transcript),
        principal_id="ben@example.org",
        tool="claude-code",
    )
    assert report["written"] == 1


def test_dispatch_opencode_raises_not_implemented(isolated_composition, tmp_path):
    from axiom.memory.session_capture import ingest_session_log

    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}")

    with pytest.raises(NotImplementedError) as excinfo:
        ingest_session_log(
            composition=isolated_composition,
            path=str(transcript),
            principal_id="ben@example.org",
            tool="opencode",
        )
    msg = str(excinfo.value).lower()
    assert "opencode" in msg
    # Points contributors to where a parser would land.
    assert "session_capture" in msg or "parser" in msg


def test_dispatch_unknown_tool_raises_value_error(isolated_composition, tmp_path):
    from axiom.memory.session_capture import ingest_session_log

    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}")

    with pytest.raises(ValueError) as excinfo:
        ingest_session_log(
            composition=isolated_composition,
            path=str(transcript),
            principal_id="ben@example.org",
            tool="something-nobody-knows",
        )
    assert "tool" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# CLI: --tool flag dispatch
# ---------------------------------------------------------------------------


def test_cli_ingest_tool_claude_code_default_works(
    monkeypatch, isolated_composition, tmp_path, capsys,
):
    from axiom.extensions.builtins.memory import cli

    monkeypatch.setattr(
        cli, "_build_default_composition", lambda: isolated_composition,
    )

    transcript = tmp_path / "session.jsonl"
    with transcript.open("w") as f:
        f.write(json.dumps({
            "type": "user", "uuid": "u-1",
            "timestamp": "2026-05-01T10:00:00Z", "sessionId": "s-1",
            "message": {"role": "user", "content": "hi"},
        }) + "\n")
        f.write(json.dumps({
            "type": "assistant", "uuid": "a-1",
            "timestamp": "2026-05-01T10:00:05Z", "sessionId": "s-1",
            "message": {"role": "assistant", "model": "x",
                        "content": [{"type": "text", "text": "hello"}]},
        }) + "\n")

    rc = cli.main([
        "ingest", str(transcript),
        "--principal", "ben@example.org",
        "--tool", "claude-code",
        "--json",
    ])
    assert rc == 0


def test_cli_ingest_tool_opencode_returns_error_with_pointer(
    monkeypatch, isolated_composition, tmp_path, capsys,
):
    from axiom.extensions.builtins.memory import cli

    monkeypatch.setattr(
        cli, "_build_default_composition", lambda: isolated_composition,
    )

    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}")

    rc = cli.main([
        "ingest", str(transcript),
        "--principal", "ben@example.org",
        "--tool", "opencode",
    ])
    assert rc != 0
    err = capsys.readouterr().err
    assert "opencode" in err.lower()
