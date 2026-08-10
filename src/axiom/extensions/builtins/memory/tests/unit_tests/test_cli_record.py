# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axi memory record` — the CLI write subcommand.

Calls the same `record_session_turn()` common path as the MCP server, so
a write via CLI is indistinguishable from a write via MCP.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest


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


@pytest.fixture
def patched_cli(monkeypatch, isolated_composition):
    """Force CLI to use isolated composition instead of ~/.axi/memory/."""
    from axiom.extensions.builtins.memory import cli
    monkeypatch.setattr(
        cli, "_build_default_composition", lambda: isolated_composition,
    )
    return isolated_composition


# ---------------------------------------------------------------------------
# axi memory record — flag-driven write
# ---------------------------------------------------------------------------


def test_record_via_flags_writes_fragment(patched_cli, capsys):
    from axiom.extensions.builtins.memory import cli
    from axiom.memory.session_summary import list_fragments_by_principal

    rc = cli.main([
        "record",
        "--principal", "ben@example.org",
        "--tool", "claude-code",
        "--model", "opus-4-7",
        "--user-input", "What is criticality?",
        "--assistant-output", "k_eff = 1.",
    ])
    assert rc == 0

    out = capsys.readouterr().out
    # CLI should print a fragment_id confirmation.
    assert "fragment" in out.lower() or "recorded" in out.lower()

    frags = list_fragments_by_principal(
        patched_cli, "ben@example.org", limit=10,
    )
    assert len(frags) == 1
    assert frags[0].content["tool"] == "claude-code"


def test_record_emits_json_when_flag_set(patched_cli, capsys):
    from axiom.extensions.builtins.memory import cli

    rc = cli.main([
        "record",
        "--principal", "ben@example.org",
        "--tool", "claude-code",
        "--user-input", "hi",
        "--assistant-output", "hello",
        "--json",
    ])
    assert rc == 0

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["principal_id"] == "ben@example.org"
    assert payload["fragment_id"]


def test_record_via_stdin_json(patched_cli, capsys, monkeypatch):
    """JSON-on-stdin path — for hooks and automation."""
    from axiom.extensions.builtins.memory import cli

    event = {
        "principal_id": "ben@example.org",
        "tool": "claude-code",
        "model": "opus-4-7",
        "user_input": "From stdin",
        "assistant_output": "OK",
        "extra": {"session_id": "s-1"},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))

    rc = cli.main(["record", "--json-stdin"])
    assert rc == 0


# Note: previously asserted that omitting --principal must fail. Once the
# memory.default_principal pin landed (Phase 1), that contract changed: the
# CLI now falls back to the pinned default. The "no principal AND no pin"
# error case is covered with proper settings isolation in
# test_principal_pin.py::test_cli_record_errors_clearly_when_no_principal_and_no_pin.
