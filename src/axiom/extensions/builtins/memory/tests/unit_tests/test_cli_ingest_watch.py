# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axi memory ingest --watch` polling-mode incremental ingest.

Watch mode wraps idempotent ingest in a polling loop so new turn pairs
appearing in a transcript get folded into the ledger as the conversation
unfolds. Tests inject a stub clock + iteration count to keep the loop
bounded.
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


def _append_turn_pair(path: Path, *, user_uuid: str, text: str) -> None:
    lines = [
        {
            "type": "user", "uuid": user_uuid,
            "timestamp": f"2026-05-01T10:0{user_uuid[-1]}:00Z",
            "sessionId": "s-1",
            "message": {"role": "user", "content": f"Q: {text}"},
        },
        {
            "type": "assistant", "uuid": f"a-{user_uuid}",
            "timestamp": f"2026-05-01T10:0{user_uuid[-1]}:05Z",
            "sessionId": "s-1",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-7",
                "content": [{"type": "text", "text": f"A: {text}"}],
            },
        },
    ]
    with path.open("a") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def test_watch_picks_up_new_turn_pairs_across_iterations(
    isolated_composition, tmp_path,
):
    """Each polling cycle re-scans + idempotently ingests new pairs."""
    from axiom.memory.session_capture import watch_ingest_claude_code_jsonl
    from axiom.memory.session_summary import list_fragments_by_principal

    transcript = tmp_path / "session.jsonl"
    transcript.touch()

    # First two turn pairs already present.
    _append_turn_pair(transcript, user_uuid="u-1", text="hi")
    _append_turn_pair(transcript, user_uuid="u-2", text="follow-up")

    # Will run 3 iterations of the loop; on iter 1 we ingest the first 2,
    # before iter 2 we append a third pair, before iter 3 a fourth.
    iter_count = {"n": 0}

    def fake_sleep(seconds):
        iter_count["n"] += 1
        if iter_count["n"] == 1:
            _append_turn_pair(transcript, user_uuid="u-3", text="third")
        elif iter_count["n"] == 2:
            _append_turn_pair(transcript, user_uuid="u-4", text="fourth")

    report = watch_ingest_claude_code_jsonl(
        composition=isolated_composition,
        path=str(transcript),
        principal_id="user@example.org",
        interval_s=0.0,
        max_iterations=3,
        sleep_fn=fake_sleep,
    )

    # 3 polling rounds total; cumulative writes = 4.
    assert report["iterations"] == 3
    assert report["total_written"] == 4

    frags = list_fragments_by_principal(
        isolated_composition, "user@example.org", limit=10,
    )
    assert len(frags) == 4


def test_watch_no_changes_writes_nothing_after_first_iteration(
    isolated_composition, tmp_path,
):
    from axiom.memory.session_capture import watch_ingest_claude_code_jsonl
    from axiom.memory.session_summary import list_fragments_by_principal

    transcript = tmp_path / "session.jsonl"
    transcript.touch()
    _append_turn_pair(transcript, user_uuid="u-1", text="hi")

    report = watch_ingest_claude_code_jsonl(
        composition=isolated_composition,
        path=str(transcript),
        principal_id="user@example.org",
        interval_s=0.0,
        max_iterations=4,
        sleep_fn=lambda _s: None,
    )

    assert report["iterations"] == 4
    assert report["total_written"] == 1   # only iteration 1 wrote anything

    frags = list_fragments_by_principal(
        isolated_composition, "user@example.org", limit=10,
    )
    assert len(frags) == 1


def test_watch_handles_missing_path_gracefully(isolated_composition, tmp_path):
    """Missing transcript at start: report scanned=0 each iteration; no crash."""
    from axiom.memory.session_capture import watch_ingest_claude_code_jsonl

    report = watch_ingest_claude_code_jsonl(
        composition=isolated_composition,
        path=str(tmp_path / "not-yet.jsonl"),
        principal_id="user@example.org",
        interval_s=0.0,
        max_iterations=2,
        sleep_fn=lambda _s: None,
    )

    assert report["iterations"] == 2
    assert report["total_written"] == 0
    assert report["total_scanned"] == 0


# ---------------------------------------------------------------------------
# CLI integration — `axi memory ingest <path> --watch`
# ---------------------------------------------------------------------------


def test_cli_ingest_watch_dispatches_through_watch_path(
    monkeypatch, isolated_composition, tmp_path, capsys,
):
    from axiom.extensions.builtins.memory import cli
    from axiom.memory.session_summary import list_fragments_by_principal

    monkeypatch.setattr(
        cli, "_build_default_composition", lambda: isolated_composition,
    )

    transcript = tmp_path / "session.jsonl"
    transcript.touch()
    _append_turn_pair(transcript, user_uuid="u-1", text="hi")

    rc = cli.main([
        "ingest", str(transcript),
        "--principal", "user@example.org",
        "--watch",
        "--interval", "0",
        "--max-iterations", "1",
        "--json",
    ])
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["iterations"] == 1
    assert payload["total_written"] == 1

    frags = list_fragments_by_principal(
        isolated_composition, "user@example.org", limit=10,
    )
    assert len(frags) == 1
