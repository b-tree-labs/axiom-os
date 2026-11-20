# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the codex (OpenAI Codex CLI) session-log parser.

Codex writes per-session JSONL rollouts at
``~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<sessionid>.jsonl``. Each line is
a `session_meta`, `event_msg`, `turn_context`, or `response_item` record.
Conversation content lives under `response_item` with
``payload.type == "message"`` and ``payload.role in {user, assistant, developer}``.
Multiple consecutive same-role records appear (the assistant streams responses
across multiple records; auto-injected user context plus user prompt arrive
as two adjacent user records).

The parser collapses consecutive same-role records into one "segment" and
emits (user_segment, assistant_segment) pairs. Developer-role records (system
permissions / instructions) are dropped. Source-uuid for idempotency is
derived deterministically from the session_meta `id` + the turn index.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
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


def _write_codex_transcript(path: Path, *, session_id: str = "sess-abc") -> None:
    """Write a small but realistic codex rollout to ``path``.

    Two-turn session. First user record carries auto-injected context
    (developer-style permissions came in a `developer`-role record that
    parser must drop); second user record is the real prompt. Assistant
    response streams across two `response_item` records that should
    collapse into one segment.
    """
    records = [
        # 0: session metadata — drives source_uuid and provenance
        {
            "timestamp": "2026-05-02T05:35:01.667Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "timestamp": "2026-05-02T05:35:01.667Z",
                "cwd": "/Users/example/Projects/Demo",
                "originator": "codex-tui",
                "cli_version": "0.128.0",
                "source": "cli",
                "model_provider": "openai",
            },
        },
        # 1: event_msg — ignored by parser
        {
            "timestamp": "2026-05-02T05:35:32.268Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "t-1"},
        },
        # 2: developer role — DROPPED by parser
        {
            "timestamp": "2026-05-02T05:35:32.268Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [
                    {"type": "input_text", "text": "<permissions instructions>..."}
                ],
            },
        },
        # 3: turn_context — ignored
        {
            "timestamp": "2026-05-02T05:35:32.268Z",
            "type": "turn_context",
            "payload": {"turn_id": "t-1", "cwd": "/Users/example/Projects/Demo"},
        },
        # 4: user — auto-injected AGENTS.md / environment_context wrapper
        {
            "timestamp": "2026-05-02T05:35:32.268Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "<environment_context>cwd=demo</environment_context>"}
                ],
            },
        },
        # 5: user — the actual prompt (collapses with previous user record)
        {
            "timestamp": "2026-05-02T05:35:32.268Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "What is 2+2?"}],
            },
        },
        # 6: assistant — streamed first part
        {
            "timestamp": "2026-05-02T05:35:34.679Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Let me compute."}],
            },
        },
        # 7: assistant — streamed continuation (collapses)
        {
            "timestamp": "2026-05-02T05:35:35.500Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "2+2 = 4."}],
            },
        },
        # 8: non-message response_item (function_call) — ignored
        {
            "timestamp": "2026-05-02T05:35:34.700Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "arguments": "{}",
                "call_id": "c1",
            },
        },
        # 9: second user turn
        {
            "timestamp": "2026-05-02T05:36:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Thanks. And 3*3?"}],
            },
        },
        # 10: second assistant turn
        {
            "timestamp": "2026-05-02T05:36:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "3*3 = 9."}],
            },
        },
    ]
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------
# parse_codex_jsonl — pure parse, no composition
# ---------------------------------------------------------------------------


def test_parse_codex_jsonl_returns_turn_pairs(tmp_path: Path):
    from axiom.memory.session_capture import parse_codex_jsonl

    path = tmp_path / "rollout.jsonl"
    _write_codex_transcript(path, session_id="sess-abc")

    pairs = parse_codex_jsonl(str(path))

    assert len(pairs) == 2


def test_parse_codex_jsonl_collapses_consecutive_user_records(tmp_path: Path):
    """Auto-injected env_context + actual prompt collapse into one user_input."""
    from axiom.memory.session_capture import parse_codex_jsonl

    path = tmp_path / "rollout.jsonl"
    _write_codex_transcript(path, session_id="sess-abc")

    pairs = parse_codex_jsonl(str(path))

    first = pairs[0]
    assert "environment_context" in first["user_input"]
    assert "What is 2+2?" in first["user_input"]


def test_parse_codex_jsonl_collapses_consecutive_assistant_records(tmp_path: Path):
    """Streamed assistant output across multiple records collapses into one."""
    from axiom.memory.session_capture import parse_codex_jsonl

    path = tmp_path / "rollout.jsonl"
    _write_codex_transcript(path)

    pairs = parse_codex_jsonl(str(path))

    first = pairs[0]
    assert "Let me compute" in first["assistant_output"]
    assert "2+2 = 4" in first["assistant_output"]


def test_parse_codex_jsonl_drops_developer_role(tmp_path: Path):
    """`role=developer` records carry system permissions — never in user_input."""
    from axiom.memory.session_capture import parse_codex_jsonl

    path = tmp_path / "rollout.jsonl"
    _write_codex_transcript(path)

    pairs = parse_codex_jsonl(str(path))

    for p in pairs:
        assert "<permissions instructions>" not in p["user_input"]


def test_parse_codex_jsonl_ignores_function_calls(tmp_path: Path):
    """`payload.type != message` records (function_call, etc) are ignored."""
    from axiom.memory.session_capture import parse_codex_jsonl

    path = tmp_path / "rollout.jsonl"
    _write_codex_transcript(path)

    pairs = parse_codex_jsonl(str(path))

    # Function call between turn 1 and turn 2 must not produce a phantom pair.
    assert len(pairs) == 2


def test_parse_codex_jsonl_carries_session_metadata(tmp_path: Path):
    """session_id, cwd, version come from session_meta."""
    from axiom.memory.session_capture import parse_codex_jsonl

    path = tmp_path / "rollout.jsonl"
    _write_codex_transcript(path, session_id="sess-xyz")

    pairs = parse_codex_jsonl(str(path))

    assert pairs[0]["session_id"] == "sess-xyz"
    assert pairs[0]["cwd"] == "/Users/example/Projects/Demo"
    assert pairs[0]["version"] == "0.128.0"


def test_parse_codex_jsonl_source_uuid_is_deterministic(tmp_path: Path):
    """Re-parsing the same file produces identical user_uuids."""
    from axiom.memory.session_capture import parse_codex_jsonl

    path = tmp_path / "rollout.jsonl"
    _write_codex_transcript(path, session_id="sess-stable")

    p1 = parse_codex_jsonl(str(path))
    p2 = parse_codex_jsonl(str(path))

    assert [p["user_uuid"] for p in p1] == [p["user_uuid"] for p in p2]
    # Distinct per turn within a file.
    assert p1[0]["user_uuid"] != p1[1]["user_uuid"]


def test_parse_codex_jsonl_source_uuid_includes_session_id(tmp_path: Path):
    """Two different sessions yield distinct uuids even at the same turn index."""
    from axiom.memory.session_capture import parse_codex_jsonl

    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    _write_codex_transcript(a, session_id="sess-A")
    _write_codex_transcript(b, session_id="sess-B")

    pa = parse_codex_jsonl(str(a))
    pb = parse_codex_jsonl(str(b))

    assert pa[0]["user_uuid"] != pb[0]["user_uuid"]


def test_parse_codex_jsonl_empty_file(tmp_path: Path):
    """An empty file parses cleanly to zero pairs."""
    from axiom.memory.session_capture import parse_codex_jsonl

    path = tmp_path / "empty.jsonl"
    path.write_text("")

    assert parse_codex_jsonl(str(path)) == []


def test_parse_codex_jsonl_tolerates_malformed_lines(tmp_path: Path):
    """Lines that aren't valid JSON are skipped, not fatal."""
    from axiom.memory.session_capture import parse_codex_jsonl

    path = tmp_path / "rollout.jsonl"
    _write_codex_transcript(path)
    # Append a malformed line; parsing should still recover prior pairs.
    with path.open("a") as f:
        f.write("not-json\n")

    pairs = parse_codex_jsonl(str(path))
    assert len(pairs) == 2


# ---------------------------------------------------------------------------
# ingest_codex_jsonl — end-to-end through CompositionService
# ---------------------------------------------------------------------------


def test_ingest_codex_jsonl_writes_fragments(isolated_composition, tmp_path: Path):
    from axiom.memory.session_capture import ingest_codex_jsonl

    path = tmp_path / "rollout.jsonl"
    _write_codex_transcript(path)

    report = ingest_codex_jsonl(
        composition=isolated_composition,
        path=str(path),
        principal_id="user@example.org",
    )

    assert report["scanned"] == 2
    assert report["written"] == 2
    assert report["skipped"] == 0


def test_ingest_codex_jsonl_is_idempotent(isolated_composition, tmp_path: Path):
    """Second ingest of the same file writes nothing — all turns skipped."""
    from axiom.memory.session_capture import ingest_codex_jsonl

    path = tmp_path / "rollout.jsonl"
    _write_codex_transcript(path)

    first = ingest_codex_jsonl(
        composition=isolated_composition,
        path=str(path),
        principal_id="user@example.org",
    )
    second = ingest_codex_jsonl(
        composition=isolated_composition,
        path=str(path),
        principal_id="user@example.org",
    )

    assert first["written"] == 2
    assert second["written"] == 0
    assert second["skipped"] == 2


def test_ingest_codex_jsonl_dry_run_writes_nothing(isolated_composition, tmp_path: Path):
    from axiom.memory.session_capture import ingest_codex_jsonl

    path = tmp_path / "rollout.jsonl"
    _write_codex_transcript(path)

    report = ingest_codex_jsonl(
        composition=isolated_composition,
        path=str(path),
        principal_id="user@example.org",
        dry_run=True,
    )

    assert report["written"] == 0
    assert report["scanned"] == 2


def test_ingest_codex_jsonl_tags_tool_as_codex(isolated_composition, tmp_path: Path):
    from axiom.memory.session_capture import ingest_codex_jsonl

    path = tmp_path / "rollout.jsonl"
    _write_codex_transcript(path)

    ingest_codex_jsonl(
        composition=isolated_composition,
        path=str(path),
        principal_id="user@example.org",
    )

    fragments = [
        a for a in isolated_composition.artifact_registry.list(kind="fragment")
    ]
    assert len(fragments) >= 2
    for a in fragments:
        content = (a.data or {}).get("content") or {}
        if content.get("fact_kind") == "chat_turn":
            assert content.get("tool") == "codex"


# ---------------------------------------------------------------------------
# Dispatch — KNOWN_TOOL_PARSERS routes --tool codex correctly
# ---------------------------------------------------------------------------


def test_registry_lists_codex():
    from axiom.memory.session_capture import KNOWN_TOOL_PARSERS

    assert "codex" in KNOWN_TOOL_PARSERS


def test_dispatch_codex_works(isolated_composition, tmp_path: Path):
    from axiom.memory.session_capture import ingest_session_log

    path = tmp_path / "rollout.jsonl"
    _write_codex_transcript(path)

    report = ingest_session_log(
        composition=isolated_composition,
        path=str(path),
        principal_id="user@example.org",
        tool="codex",
    )

    assert report["written"] == 2
