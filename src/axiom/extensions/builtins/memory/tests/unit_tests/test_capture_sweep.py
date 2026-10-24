# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``memory.capture-sweep`` — the cross-tool capture backstop.

Replaces the hand-rolled launchd shell script whose success criterion was
"how many files did I open". That counter reported ``ok: ingested 4
file(s), 0 error(s)`` every hour for four days while writing zero
fragments, because every turn deduped against an existing uuid. A run
that stores nothing is not a successful run, and the sweep has to be able
to say so.

What the sweep owes its caller:

- sweep every known harness, not just the one that happens to be open
- report **fragments written**, per tool, not files touched
- surface ledger staleness as a non-ok result so a watchdog can alert
- never fail the whole sweep because one harness is absent or corrupt
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _recent(minutes_ago: int = 5) -> str:
    """A timestamp near now.

    These fixtures feed the staleness alarm, so hardcoded dates rot: a
    suite that passes today fails tomorrow once the fixture drifts past
    the 24h threshold. Anchor to now instead.
    """
    return (
        datetime.now(UTC) - timedelta(minutes=minutes_ago)
    ).isoformat().replace("+00:00", "Z")


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


def _claude_transcript(path: Path, uuid: str = "u-1") -> None:
    lines = [
        {
            "type": "user", "uuid": uuid, "sessionId": "s-1",
            "timestamp": _recent(6), "cwd": "/repo",
            "message": {"role": "user",
                        "content": "Always pin the service venv, never the editable one."},
        },
        {
            "type": "assistant", "uuid": "a-1", "sessionId": "s-1",
            "timestamp": _recent(5),
            "message": {"role": "assistant", "model": "claude-opus-5",
                        "content": [{"type": "text",
                                     "text": "Pinned it to the non-editable install."}]},
        },
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def _cursor_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
    conn.executemany(
        "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
        [
            ("composerData:c-1", json.dumps({"composerId": "c-1", "createdAt": 1})),
            ("bubbleId:c-1:b-1", json.dumps({
                "bubbleId": "b-1", "type": 1,
                "text": "Should the object tier use SeaweedFS instead of MinIO?",
                "createdAt": _recent(4),
                "modelInfo": {"modelName": "grok-4.6"},
            })),
            ("bubbleId:c-1:b-2", json.dumps({
                "bubbleId": "b-2", "type": 2,
                "text": "Yes — SeaweedFS is the standing choice for the object tier.",
                "createdAt": _recent(3),
            })),
        ],
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Sweep covers every harness
# ---------------------------------------------------------------------------


def test_sweep_captures_claude_code_and_cursor_in_one_pass(
    tmp_path: Path, isolated_composition,
):
    """One sweep, both surfaces — the whole point of cross-tool memory."""
    from axiom.extensions.builtins.memory.skills.capture_sweep import capture_sweep

    projects = tmp_path / "projects" / "proj"
    projects.mkdir(parents=True)
    _claude_transcript(projects / "s.jsonl")

    cursor_db = tmp_path / "state.vscdb"
    _cursor_db(cursor_db)

    result = capture_sweep({
        "composition": isolated_composition,
        "principal": "ben@example.org",
        "claude_projects": str(tmp_path / "projects"),
        "cursor_paths": [str(cursor_db)],
        "codex_sessions": str(tmp_path / "nonexistent-codex"),
    }, None)

    assert result.ok, result.errors
    by_tool = result.value["written_by_tool"]
    assert by_tool["claude-code"] == 1
    assert by_tool["cursor"] == 1
    assert result.value["written"] == 2


def test_sweep_reports_fragments_not_files(tmp_path: Path, isolated_composition):
    """The exact metric the old shell watchdog got wrong."""

    projects = tmp_path / "projects" / "proj"
    projects.mkdir(parents=True)
    _claude_transcript(projects / "s.jsonl")

    params = {
        "composition": isolated_composition,
        "principal": "ben@example.org",
        "claude_projects": str(tmp_path / "projects"),
        "cursor_paths": [],
        "codex_sessions": str(tmp_path / "none"),
    }
    from axiom.extensions.builtins.memory.skills.capture_sweep import capture_sweep as sweep

    first = sweep(dict(params), None)
    assert first.value["written"] == 1

    # Second pass touches the same file and writes nothing new. The old
    # script called this "ok: ingested 1 file(s)"; the sweep must call it
    # zero fragments written.
    second = sweep(dict(params), None)
    assert second.value["files_scanned"] >= 1
    assert second.value["written"] == 0


def test_missing_harness_does_not_fail_the_sweep(tmp_path: Path, isolated_composition):
    """Cursor not installed is normal, not an error."""
    from axiom.extensions.builtins.memory.skills.capture_sweep import capture_sweep

    projects = tmp_path / "projects" / "proj"
    projects.mkdir(parents=True)
    _claude_transcript(projects / "s.jsonl")

    result = capture_sweep({
        "composition": isolated_composition,
        "principal": "ben@example.org",
        "claude_projects": str(tmp_path / "projects"),
        "cursor_paths": [str(tmp_path / "no-such-cursor.vscdb")],
        "codex_sessions": str(tmp_path / "none"),
    }, None)

    assert result.ok
    assert result.value["written"] == 1


def test_corrupt_source_is_reported_but_sweep_continues(
    tmp_path: Path, isolated_composition,
):
    from axiom.extensions.builtins.memory.skills.capture_sweep import capture_sweep

    projects = tmp_path / "projects" / "proj"
    projects.mkdir(parents=True)
    _claude_transcript(projects / "good.jsonl")
    (projects / "bad.jsonl").write_text("{not json at all\n")

    result = capture_sweep({
        "composition": isolated_composition,
        "principal": "ben@example.org",
        "claude_projects": str(tmp_path / "projects"),
        "cursor_paths": [],
        "codex_sessions": str(tmp_path / "none"),
    }, None)

    assert result.value["written"] == 1


def test_subagent_transcripts_are_not_swept(tmp_path: Path, isolated_composition):
    """Subagent traffic is the agent talking to itself."""
    from axiom.extensions.builtins.memory.skills.capture_sweep import capture_sweep

    projects = tmp_path / "projects" / "proj"
    sub = projects / "subagents"
    sub.mkdir(parents=True)
    _claude_transcript(sub / "s.jsonl")

    result = capture_sweep({
        "composition": isolated_composition,
        "principal": "ben@example.org",
        "claude_projects": str(tmp_path / "projects"),
        "cursor_paths": [],
        "codex_sessions": str(tmp_path / "none"),
    }, None)

    assert result.value["written"] == 0


# ---------------------------------------------------------------------------
# Staleness is the alarm the old watchdog could not raise
# ---------------------------------------------------------------------------


def test_stale_ledger_is_not_ok(tmp_path: Path, isolated_composition):
    """Nothing captured and nothing in the ledger for days ⇒ alarm."""
    from axiom.extensions.builtins.memory.skills.capture_sweep import capture_sweep

    result = capture_sweep({
        "composition": isolated_composition,
        "principal": "ben@example.org",
        "claude_projects": str(tmp_path / "none"),
        "cursor_paths": [],
        "codex_sessions": str(tmp_path / "none"),
        "max_ledger_age_hours": 24,
    }, None)

    assert not result.ok
    assert any("stale" in e or "no chat_turn" in e for e in result.errors)


def test_fresh_capture_clears_the_staleness_alarm(
    tmp_path: Path, isolated_composition,
):
    from axiom.extensions.builtins.memory.skills.capture_sweep import capture_sweep

    projects = tmp_path / "projects" / "proj"
    projects.mkdir(parents=True)
    lines = [
        {
            "type": "user", "uuid": "u-1", "sessionId": "s-1",
            "timestamp": "2099-01-01T00:00:00.000Z",
            "message": {"role": "user",
                        "content": "Always pin the service venv, never the editable one."},
        },
        {
            "type": "assistant", "uuid": "a-1", "sessionId": "s-1",
            "timestamp": "2099-01-01T00:00:05.000Z",
            "message": {"role": "assistant", "model": "m",
                        "content": [{"type": "text",
                                     "text": "Pinned to the non-editable install."}]},
        },
    ]
    (projects / "s.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")

    result = capture_sweep({
        "composition": isolated_composition,
        "principal": "ben@example.org",
        "claude_projects": str(tmp_path / "projects"),
        "cursor_paths": [],
        "codex_sessions": str(tmp_path / "none"),
        "max_ledger_age_hours": 24,
    }, None)

    assert result.ok, result.errors


# ---------------------------------------------------------------------------
# Hollow-fragment repair
# ---------------------------------------------------------------------------


def test_repair_purges_hollow_fragments(isolated_composition):
    from axiom.extensions.builtins.memory.skills.repair_hollow import repair_hollow
    from axiom.memory.session_capture import record_session_turn

    for i in range(3):
        record_session_turn(
            composition=isolated_composition,
            principal_id="ben@example.org",
            tool="claude-code", model="m",
            user_input="", assistant_output="",
            extra={"source_uuid": f"u-{i}"},
        )
    record_session_turn(
        composition=isolated_composition,
        principal_id="ben@example.org",
        tool="claude-code", model="m",
        user_input="Always pin the service venv.",
        assistant_output="Pinned to the non-editable install.",
        extra={"source_uuid": "keep-1"},
    )

    result = repair_hollow({
        "composition": isolated_composition,
        "principal": "ben@example.org",
    }, None)

    assert result.ok
    assert result.value["purged"] == 3
    assert result.value["kept"] == 1


def test_repair_dry_run_changes_nothing(isolated_composition):
    from axiom.extensions.builtins.memory.skills.repair_hollow import repair_hollow
    from axiom.memory.session_capture import record_session_turn

    record_session_turn(
        composition=isolated_composition,
        principal_id="ben@example.org",
        tool="claude-code", model="m",
        user_input="", assistant_output="",
        extra={"source_uuid": "u-0"},
    )

    result = repair_hollow({
        "composition": isolated_composition,
        "principal": "ben@example.org",
        "dry_run": True,
    }, None)

    assert result.value["would_purge"] == 1
    # Still there.
    second = repair_hollow({
        "composition": isolated_composition,
        "principal": "ben@example.org",
        "dry_run": True,
    }, None)
    assert second.value["would_purge"] == 1


def test_zero_threshold_is_honoured_not_defaulted(tmp_path: Path, isolated_composition):
    """0 is a real threshold ("must be fresh now"), not a missing value."""
    from axiom.extensions.builtins.memory.skills.capture_sweep import capture_sweep
    from axiom.memory.session_capture import record_session_turn

    record_session_turn(
        composition=isolated_composition,
        principal_id="ben@example.org",
        tool="claude-code", model="m",
        user_input="Always pin the service venv.",
        assistant_output="Pinned to the non-editable install.",
        extra={"source_uuid": "u-1"},
    )

    result = capture_sweep({
        "composition": isolated_composition,
        "principal": "ben@example.org",
        "claude_projects": str(tmp_path / "none"),
        "cursor_paths": [],
        "codex_sessions": str(tmp_path / "none"),
        "max_ledger_age_hours": 0,
    }, None)

    # A just-written turn is still >0h old by a hair, so a 0h threshold
    # must trip. If 0 were coalesced to the 24h default this would pass.
    assert not result.ok
    assert any("stale" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Mid-answer partials — captured in flight, then locked by the dedupe
# ---------------------------------------------------------------------------


def _partial(composition, session_id: str, uuid: str) -> None:
    from axiom.memory.session_capture import record_session_turn

    record_session_turn(
        composition=composition, principal_id="ben@example.org",
        tool="claude-code", model="m",
        user_input="Why did the ingest sweep write nothing this run?",
        assistant_output="",
        extra={"source_uuid": uuid, "session_id": session_id},
    )


def test_partials_are_left_alone_by_default(tmp_path: Path, isolated_composition):
    """Purging is opt-in; the plain verb only touches hollow fragments."""
    from axiom.extensions.builtins.memory.skills.repair_hollow import repair_hollow

    _partial(isolated_composition, "s-1", "u-1")

    result = repair_hollow({
        "composition": isolated_composition,
        "principal": "ben@example.org",
        "dry_run": True,
    }, None)
    assert result.value["would_purge"] == 0


def test_partial_with_surviving_transcript_is_recoverable(
    tmp_path: Path, isolated_composition,
):
    from axiom.extensions.builtins.memory.skills.repair_hollow import repair_hollow

    projects = tmp_path / "projects" / "proj"
    projects.mkdir(parents=True)
    (projects / "s-1.jsonl").write_text("")
    _partial(isolated_composition, "s-1", "u-1")

    result = repair_hollow({
        "composition": isolated_composition,
        "principal": "ben@example.org",
        "include_partial": True,
        "claude_projects": str(tmp_path / "projects"),
    }, None)
    assert result.value["purged_partial"] == 1


def test_partial_without_transcript_is_kept(tmp_path: Path, isolated_composition):
    """Never trade a half turn for nothing at all."""
    from axiom.extensions.builtins.memory.skills.repair_hollow import repair_hollow

    (tmp_path / "projects").mkdir()
    _partial(isolated_composition, "gone-session", "u-1")

    result = repair_hollow({
        "composition": isolated_composition,
        "principal": "ben@example.org",
        "include_partial": True,
        "claude_projects": str(tmp_path / "projects"),
    }, None)
    assert result.value["purged_partial"] == 0
    assert result.value["partial_unrecoverable"] == 1


def test_backfilled_prompts_are_never_treated_as_partial(
    tmp_path: Path, isolated_composition,
):
    """prompt_only turns are intentional half turns, not capture failures."""
    from axiom.extensions.builtins.memory.skills.repair_hollow import repair_hollow
    from axiom.memory.session_capture import record_session_turn

    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "s-1.jsonl").write_text("")
    record_session_turn(
        composition=isolated_composition, principal_id="ben@example.org",
        tool="claude-code", model="",
        user_input="Recovered prompt from a deleted transcript.",
        assistant_output="",
        extra={"source_uuid": "history:abc", "session_id": "s-1",
               "capture_mode": "prompt_only"},
    )

    result = repair_hollow({
        "composition": isolated_composition,
        "principal": "ben@example.org",
        "include_partial": True,
        "claude_projects": str(projects),
    }, None)
    assert result.value["purged_partial"] == 0
