# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Cursor session capture — the cross-tool hole found 2026-08-18.

Cursor keeps chat in a VS Code-style SQLite store at
``~/Library/Application Support/Cursor/User/globalStorage/state.vscdb``,
table ``cursorDiskKV``:

- ``composerData:<composerId>`` — one conversation (createdAt epoch ms)
- ``bubbleId:<composerId>:<bubbleId>`` — one message. ``type`` 1 = user,
  2 = assistant; ``text`` is the prose; ``createdAt`` is ISO-8601;
  ``modelInfo.modelName`` names the model the user had selected.

The model is whatever Cursor is configured with (grok-4.6, gpt-*,
claude-*), so the adapter must never assume a vendor — it records
whatever ``modelName`` says and stamps ``tool="cursor"``. That is the
whole point: the ledger has to fill the same way whether Ben is in
Cursor with Grok or in Claude Code with Opus.

Cursor holds the DB open with WAL, so the reader must not mutate it.
"""

from __future__ import annotations

import json
import sqlite3
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


def _make_cursor_db(path: Path, rows: list[tuple[str, dict]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
    conn.executemany(
        "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
        [(k, json.dumps(v)) for k, v in rows],
    )
    conn.commit()
    conn.close()


def _grok_session(path: Path) -> None:
    """A Cursor agent session run against grok-4.6, with tool bubbles."""
    cid = "c-1"
    _make_cursor_db(path, [
        (f"composerData:{cid}", {
            "composerId": cid, "createdAt": 1777244105759,
            "name": "ROC proposal", "unifiedMode": "agent",
        }),
        (f"bubbleId:{cid}:b-1", {
            "bubbleId": "b-1", "type": 1,
            "text": "Should the PXI backpack use SeaweedFS for the object tier?",
            "createdAt": "2026-08-17T18:18:25.004Z",
            "modelInfo": {"modelName": "grok-4.6"},
        }),
        # agent/tool bubble — no prose, must not become a fragment
        (f"bubbleId:{cid}:b-2", {
            "bubbleId": "b-2", "type": 2, "text": "",
            "createdAt": "2026-08-17T18:18:28.000Z",
            "toolResults": [{"name": "read_file"}],
        }),
        (f"bubbleId:{cid}:b-3", {
            "bubbleId": "b-3", "type": 2,
            "text": "Yes — SeaweedFS is the standing choice; MinIO is ruled out.",
            "createdAt": "2026-08-17T18:18:31.539Z",
        }),
    ])


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parses_user_and_assistant_bubbles(tmp_path: Path):
    from axiom.memory.session_capture import parse_cursor_state

    db = tmp_path / "state.vscdb"
    _grok_session(db)

    turns = parse_cursor_state(str(db))

    assert len(turns) == 1
    t = turns[0]
    assert t["user_input"].startswith("Should the PXI backpack")
    assert "SeaweedFS is the standing choice" in t["assistant_output"]
    assert t["model"] == "grok-4.6"
    assert t["session_id"] == "c-1"
    assert t["user_uuid"] == "b-1"


def test_empty_tool_bubbles_do_not_become_turns(tmp_path: Path):
    from axiom.memory.session_capture import parse_cursor_state

    db = tmp_path / "state.vscdb"
    _grok_session(db)

    for t in parse_cursor_state(str(db)):
        assert (t["user_input"].strip() or t["assistant_output"].strip())


def test_model_is_recorded_verbatim_not_assumed(tmp_path: Path):
    """Whatever LLM Cursor is pointed at is what gets recorded."""
    from axiom.memory.session_capture import parse_cursor_state

    db = tmp_path / "state.vscdb"
    _make_cursor_db(db, [
        ("composerData:c-9", {"composerId": "c-9", "createdAt": 1777244105759}),
        ("bubbleId:c-9:b-1", {
            "bubbleId": "b-1", "type": 1, "text": "Refactor the ingest loop.",
            "createdAt": "2026-08-18T09:00:00.000Z",
            "modelInfo": {"modelName": "gpt-5.2-codex"},
        }),
        ("bubbleId:c-9:b-2", {
            "bubbleId": "b-2", "type": 2, "text": "Rewrote it as a generator.",
            "createdAt": "2026-08-18T09:00:10.000Z",
        }),
    ])

    assert parse_cursor_state(str(db))[0]["model"] == "gpt-5.2-codex"


def test_reader_does_not_mutate_the_source_db(tmp_path: Path):
    """Cursor owns this file and may hold it open — read-only, always."""
    from axiom.memory.session_capture import parse_cursor_state

    db = tmp_path / "state.vscdb"
    _grok_session(db)
    before = db.read_bytes()

    parse_cursor_state(str(db))

    assert db.read_bytes() == before
    assert not (tmp_path / "state.vscdb-wal").exists()


def test_turns_are_ordered_by_time(tmp_path: Path):
    from axiom.memory.session_capture import parse_cursor_state

    db = tmp_path / "state.vscdb"
    _make_cursor_db(db, [
        ("composerData:c-1", {"composerId": "c-1", "createdAt": 1}),
        # inserted out of order on purpose
        ("bubbleId:c-1:b-3", {
            "bubbleId": "b-3", "type": 1, "text": "Second real question here.",
            "createdAt": "2026-08-18T12:00:00.000Z",
        }),
        ("bubbleId:c-1:b-1", {
            "bubbleId": "b-1", "type": 1, "text": "First real question here.",
            "createdAt": "2026-08-18T10:00:00.000Z",
        }),
        ("bubbleId:c-1:b-2", {
            "bubbleId": "b-2", "type": 2, "text": "Answer to the first one.",
            "createdAt": "2026-08-18T10:00:05.000Z",
        }),
        ("bubbleId:c-1:b-4", {
            "bubbleId": "b-4", "type": 2, "text": "Answer to the second one.",
            "createdAt": "2026-08-18T12:00:05.000Z",
        }),
    ])

    turns = parse_cursor_state(str(db))
    assert [t["user_uuid"] for t in turns] == ["b-1", "b-3"]
    assert "first one" in turns[0]["assistant_output"]


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def test_ingest_stamps_cursor_provenance(tmp_path: Path, isolated_composition):
    from axiom.memory.session_capture import ingest_cursor_state
    from axiom.memory.session_summary import list_fragments_by_principal

    db = tmp_path / "state.vscdb"
    _grok_session(db)

    report = ingest_cursor_state(
        composition=isolated_composition, path=str(db),
        principal_id="ben@example.org",
    )
    assert report["written"] == 1

    frags = list_fragments_by_principal(
        isolated_composition, "ben@example.org", limit=10,
    )
    content = frags[0].content
    assert content["tool"] == "cursor"
    assert content["model"] == "grok-4.6"
    assert content["user_input"]
    assert content["assistant_output"]


def test_cursor_ingest_is_idempotent(tmp_path: Path, isolated_composition):
    from axiom.memory.session_capture import ingest_cursor_state

    db = tmp_path / "state.vscdb"
    _grok_session(db)

    kw = dict(
        composition=isolated_composition, path=str(db),
        principal_id="ben@example.org",
    )
    assert ingest_cursor_state(**kw)["written"] == 1
    assert ingest_cursor_state(**kw)["written"] == 0


def test_cursor_is_registered_as_a_known_tool():
    from axiom.memory.session_capture import KNOWN_TOOL_PARSERS

    assert "cursor" in KNOWN_TOOL_PARSERS


def test_ingest_session_log_dispatches_cursor(tmp_path: Path, isolated_composition):
    from axiom.memory.session_capture import ingest_session_log

    db = tmp_path / "state.vscdb"
    _grok_session(db)

    report = ingest_session_log(
        composition=isolated_composition, path=str(db),
        principal_id="ben@example.org", tool="cursor",
    )
    assert report["written"] == 1


def test_default_cursor_store_path_is_discoverable():
    """The sweep must be able to find Cursor without being told where."""
    from axiom.memory.session_capture import default_cursor_store_paths

    paths = default_cursor_store_paths(home=Path("/home/x"))
    assert any("state.vscdb" in str(p) for p in paths)


def test_model_is_inherited_when_set_later_in_the_thread(tmp_path: Path):
    """Cursor stamps modelInfo on one bubble; the turn must not go anonymous."""
    from axiom.memory.session_capture import parse_cursor_state

    db = tmp_path / "state.vscdb"
    _make_cursor_db(db, [
        ("composerData:c-3", {"composerId": "c-3", "createdAt": 1}),
        # first user bubble carries no modelInfo at all
        ("bubbleId:c-3:b-1", {
            "bubbleId": "b-1", "type": 1, "text": "Consolidate the site repos.",
            "createdAt": "2026-08-18T10:00:00.000Z",
        }),
        ("bubbleId:c-3:b-2", {
            "bubbleId": "b-2", "type": 2,
            "text": "There are two clones; I will fold them into one.",
            "createdAt": "2026-08-18T10:00:05.000Z",
            "modelInfo": {"modelName": "grok-4.6"},
        }),
    ])

    assert parse_cursor_state(str(db))[0]["model"] == "grok-4.6"


def test_cursor_in_flight_turn_is_not_captured_as_finished(tmp_path: Path):
    """Same mid-flight hazard as Claude Code — Cursor is sweep-only."""
    from axiom.memory.session_capture import parse_cursor_state

    db = tmp_path / "state.vscdb"
    _make_cursor_db(db, [
        ("composerData:c-5", {"composerId": "c-5", "createdAt": 1}),
        ("bubbleId:c-5:b-1", {
            "bubbleId": "b-1", "type": 1,
            "text": "Should the object tier use SeaweedFS instead of MinIO?",
            "createdAt": "2026-08-19T10:00:00.000Z",
            "modelInfo": {"modelName": "grok-4.6"},
        }),
        # assistant bubble exists but has produced no prose yet
        ("bubbleId:c-5:b-2", {
            "bubbleId": "b-2", "type": 2, "text": "",
            "createdAt": "2026-08-19T10:00:02.000Z",
        }),
    ])

    assert parse_cursor_state(str(db)) == []


def test_cursor_agent_tool_calls_are_recorded(tmp_path: Path):
    """Cursor logs agent tools in toolFormerData, not toolResults.

    Missing them made tool-heavy Cursor turns indistinguishable from
    empty half-captures.
    """
    from axiom.memory.session_capture import parse_cursor_state

    db = tmp_path / "state.vscdb"
    _make_cursor_db(db, [
        ("composerData:c-7", {"composerId": "c-7", "createdAt": 1}),
        ("bubbleId:c-7:b-1", {
            "bubbleId": "b-1", "type": 1,
            "text": "Find every place we still reference MinIO.",
            "createdAt": "2026-08-19T10:00:00.000Z",
            "modelInfo": {"modelName": "grok-4.6"},
        }),
        ("bubbleId:c-7:b-2", {
            "bubbleId": "b-2", "type": 2, "text": "",
            "createdAt": "2026-08-19T10:00:02.000Z",
            "toolFormerData": {"name": "grep_search", "status": "completed"},
        }),
        ("bubbleId:c-7:b-3", {
            "bubbleId": "b-3", "type": 2,
            "text": "Three references left, all in docs.",
            "createdAt": "2026-08-19T10:00:08.000Z",
            "toolFormerData": {"name": "read_file", "status": "completed"},
        }),
    ])

    turn = parse_cursor_state(str(db))[0]
    assert turn["tools_used"] == ["grep_search", "read_file"]
