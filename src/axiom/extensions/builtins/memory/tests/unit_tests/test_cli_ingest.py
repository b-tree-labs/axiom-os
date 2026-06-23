# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axi memory ingest` — backstop for cross-tool capture.

Reads Claude Code session JSONL transcripts and folds turn pairs into the
ledger via the same `record_session_turn` common path the MCP write tool
and `axi memory record` use. No idempotency in the MVP — re-running may
duplicate. Daemon/watch mode is post-Prague.
"""

from __future__ import annotations

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
    from axiom.extensions.builtins.memory import cli
    monkeypatch.setattr(
        cli, "_build_default_composition", lambda: isolated_composition,
    )
    return isolated_composition


def _write_synthetic_transcript(path: Path) -> None:
    """Write a 2-turn Claude Code transcript with the keys we depend on."""
    lines = [
        # housekeeping line, ignored
        {"type": "permission-mode", "permissionMode": "default", "sessionId": "s-1"},
        # Turn 1
        {
            "type": "user", "uuid": "u-1", "timestamp": "2026-05-01T10:00:00Z",
            "sessionId": "s-1", "cwd": "/Users/example",
            "message": {"role": "user", "content": "What is k_eff?"},
        },
        {
            "type": "assistant", "uuid": "a-1", "timestamp": "2026-05-01T10:00:05Z",
            "sessionId": "s-1",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-7",
                "content": [
                    {"type": "thinking", "thinking": "...", "signature": "x"},
                    {"type": "text", "text": "k_eff is the effective multiplication factor."},
                ],
            },
        },
        # Turn 2
        {
            "type": "user", "uuid": "u-2", "timestamp": "2026-05-01T10:01:00Z",
            "sessionId": "s-1",
            "message": {"role": "user", "content": "And criticality?"},
        },
        {
            "type": "assistant", "uuid": "a-2", "timestamp": "2026-05-01T10:01:05Z",
            "sessionId": "s-1",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-7",
                "content": [{"type": "text", "text": "Critical means k_eff = 1."}],
            },
        },
    ]
    with path.open("w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


# ---------------------------------------------------------------------------
# Ingest writes fragments via the common path
# ---------------------------------------------------------------------------


def test_ingest_writes_fragment_per_turn_pair(patched_cli, tmp_path, capsys):
    from axiom.extensions.builtins.memory import cli
    from axiom.memory.session_summary import list_fragments_by_principal

    transcript = tmp_path / "session.jsonl"
    _write_synthetic_transcript(transcript)

    rc = cli.main([
        "ingest", str(transcript),
        "--principal", "user@example.org",
        "--json",
    ])
    assert rc == 0

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["written"] == 2
    assert payload["scanned"] == 2  # 2 turn pairs

    frags = list_fragments_by_principal(
        patched_cli, "user@example.org", limit=10,
    )
    assert len(frags) == 2

    # Provenance carries the originating tool + model.
    tools = {f.content["tool"] for f in frags}
    assert tools == {"claude-code"}
    models = {f.content["model"] for f in frags}
    assert "claude-opus-4-7" in models

    # Source uuid preserved in extra metadata for future dedup.
    source_uuids = {f.content.get("extra", {}).get("source_uuid") for f in frags}
    assert {"u-1", "u-2"}.issubset(source_uuids)


def test_ingest_dry_run_writes_nothing(patched_cli, tmp_path, capsys):
    from axiom.extensions.builtins.memory import cli
    from axiom.memory.session_summary import list_fragments_by_principal

    transcript = tmp_path / "session.jsonl"
    _write_synthetic_transcript(transcript)

    rc = cli.main([
        "ingest", str(transcript),
        "--principal", "user@example.org",
        "--dry-run", "--json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["written"] == 0
    assert payload["scanned"] == 2

    frags = list_fragments_by_principal(
        patched_cli, "user@example.org", limit=10,
    )
    assert len(frags) == 0


def test_ingest_missing_path_is_error(patched_cli, tmp_path, capsys):
    from axiom.extensions.builtins.memory import cli

    rc = cli.main([
        "ingest", str(tmp_path / "does-not-exist.jsonl"),
        "--principal", "user@example.org",
    ])
    assert rc != 0
    err = capsys.readouterr().err
    assert "not found" in err.lower() or "no such" in err.lower()


# ---------------------------------------------------------------------------
# Idempotent re-ingest — keyed on content.extra.source_uuid
# ---------------------------------------------------------------------------


def test_ingest_idempotent_on_repeat(patched_cli, tmp_path, capsys):
    """Re-running ingest on the same transcript writes nothing the second time."""
    from axiom.extensions.builtins.memory import cli
    from axiom.memory.session_summary import list_fragments_by_principal

    transcript = tmp_path / "session.jsonl"
    _write_synthetic_transcript(transcript)

    # First ingest writes 2 fragments.
    rc = cli.main([
        "ingest", str(transcript),
        "--principal", "user@example.org", "--json",
    ])
    assert rc == 0
    payload1 = json.loads(capsys.readouterr().out)
    assert payload1["written"] == 2
    assert payload1["skipped"] == 0

    # Second ingest writes nothing; reports skipped=2.
    rc = cli.main([
        "ingest", str(transcript),
        "--principal", "user@example.org", "--json",
    ])
    assert rc == 0
    payload2 = json.loads(capsys.readouterr().out)
    assert payload2["scanned"] == 2
    assert payload2["written"] == 0
    assert payload2["skipped"] == 2

    frags = list_fragments_by_principal(
        patched_cli, "user@example.org", limit=10,
    )
    assert len(frags) == 2  # not 4


def test_ingest_appends_only_new_turns_after_partial_session(
    patched_cli, tmp_path, capsys,
):
    """If a transcript grows, re-ingesting writes only the new turn pairs."""
    from axiom.extensions.builtins.memory import cli
    from axiom.memory.session_summary import list_fragments_by_principal

    transcript = tmp_path / "session.jsonl"
    _write_synthetic_transcript(transcript)

    rc = cli.main([
        "ingest", str(transcript),
        "--principal", "user@example.org", "--json",
    ])
    assert rc == 0
    capsys.readouterr()  # discard

    # Append a new turn pair to the transcript.
    new_lines = [
        {
            "type": "user", "uuid": "u-3", "timestamp": "2026-05-01T10:02:00Z",
            "sessionId": "s-1",
            "message": {"role": "user", "content": "Tell me about MSRs."},
        },
        {
            "type": "assistant", "uuid": "a-3", "timestamp": "2026-05-01T10:02:05Z",
            "sessionId": "s-1",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-7",
                "content": [{"type": "text", "text": "Molten Salt Reactors..."}],
            },
        },
    ]
    with transcript.open("a") as f:
        for line in new_lines:
            f.write(json.dumps(line) + "\n")

    rc = cli.main([
        "ingest", str(transcript),
        "--principal", "user@example.org", "--json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scanned"] == 3
    assert payload["written"] == 1
    assert payload["skipped"] == 2

    frags = list_fragments_by_principal(
        patched_cli, "user@example.org", limit=10,
    )
    assert len(frags) == 3  # 2 from first ingest + 1 new
