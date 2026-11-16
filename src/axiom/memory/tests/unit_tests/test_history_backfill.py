# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Prompt-history backfill — recovering what outlived the transcripts.

Claude Code deletes session transcripts on a rolling retention window
(``cleanupPeriodDays``, 30 by default). Because capture was a periodic
*scrape* of those transcripts rather than a write at conversation time,
everything older than the window was gone before the ledger ever saw it.

``~/.claude/history.jsonl`` survives that deletion. It holds one record
per prompt the user typed — ``display``, ``project``, ``sessionId``,
``timestamp`` — going back far earlier than any surviving transcript.
It is the only remaining record of months of work.

These are deliberately **half turns**: the user's side with no assistant
response, since the responses really are gone. They are marked
``extra.capture_mode = "prompt_only"`` so recall can tell a recovered
prompt from a full exchange, and so a later pass can never mistake one
for a hollow fragment.

Two properties matter most here:

- **Deterministic ids.** history.jsonl has no per-record uuid, so the
  source uuid is derived from the record's own content. Re-running the
  backfill must be a no-op, not a duplicate.
- **No double-capture.** Prompts whose full exchange was already
  ingested from a transcript must not be re-added as half turns.
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


def _history(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


_SUBSTANTIVE = (
    "Use SeaweedFS for the object tier instead of MinIO, and never "
    "hardcode the schema on tables."
)


def _default_rows() -> list[dict]:
    return [
        {"display": _SUBSTANTIVE, "project": "/repo",
         "sessionId": "s-1", "timestamp": 1765831102276, "pastedContents": {}},
        {"display": "pwd", "project": "/repo",
         "sessionId": "s-1", "timestamp": 1765831193418, "pastedContents": {}},
    ]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parses_prompts_with_time_and_project(tmp_path: Path):
    from axiom.memory.session_capture import parse_claude_code_history

    p = tmp_path / "history.jsonl"
    _history(p, _default_rows())

    turns = parse_claude_code_history(str(p))

    assert len(turns) == 2
    first = turns[0]
    assert first["user_input"] == _SUBSTANTIVE
    assert first["assistant_output"] == ""
    assert first["session_id"] == "s-1"
    assert first["cwd"] == "/repo"
    # epoch millis -> ISO 8601 UTC
    assert first["timestamp"].startswith("2025-12-15T")
    assert first["timestamp"].endswith("+00:00")


def test_source_uuid_is_deterministic(tmp_path: Path):
    """No uuid in the source, so it must be derived and stable."""
    from axiom.memory.session_capture import parse_claude_code_history

    p = tmp_path / "history.jsonl"
    _history(p, _default_rows())

    first = parse_claude_code_history(str(p))
    second = parse_claude_code_history(str(p))
    assert [t["user_uuid"] for t in first] == [t["user_uuid"] for t in second]
    assert all(t["user_uuid"] for t in first)
    # distinct records get distinct ids
    assert first[0]["user_uuid"] != first[1]["user_uuid"]


def test_identical_prompt_in_different_sessions_is_not_collapsed(tmp_path: Path):
    from axiom.memory.session_capture import parse_claude_code_history

    p = tmp_path / "history.jsonl"
    _history(p, [
        {"display": "run the tests", "project": "/repo",
         "sessionId": "s-1", "timestamp": 1765831102276},
        {"display": "run the tests", "project": "/repo",
         "sessionId": "s-2", "timestamp": 1765831102276},
    ])

    turns = parse_claude_code_history(str(p))
    assert turns[0]["user_uuid"] != turns[1]["user_uuid"]


def test_malformed_lines_are_skipped(tmp_path: Path):
    from axiom.memory.session_capture import parse_claude_code_history

    p = tmp_path / "history.jsonl"
    p.write_text(
        json.dumps(_default_rows()[0]) + "\n"
        "{ not json\n"
        "\n"
        + json.dumps({"display": "", "sessionId": "s-9", "timestamp": 1}) + "\n"
    )

    turns = parse_claude_code_history(str(p))
    assert len(turns) == 1  # blank display dropped too


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def test_backfill_marks_prompt_only_and_is_not_hollow(
    tmp_path: Path, isolated_composition,
):
    from axiom.memory.session_capture import (
        ingest_claude_code_history,
        is_hollow_content,
    )
    from axiom.memory.session_summary import list_fragments_by_principal

    p = tmp_path / "history.jsonl"
    _history(p, _default_rows())

    report = ingest_claude_code_history(
        composition=isolated_composition, path=str(p),
        principal_id="ben@example.org",
    )
    assert report["written"] == 1  # "pwd" filtered as noise

    frag = list_fragments_by_principal(
        isolated_composition, "ben@example.org", limit=5,
    )[0]
    assert frag.content["extra"]["capture_mode"] == "prompt_only"
    assert frag.content["tool"] == "claude-code"
    assert not is_hollow_content(frag.content), (
        "a recovered prompt is real content, not a hollow shell"
    )


def test_backfill_is_idempotent(tmp_path: Path, isolated_composition):
    from axiom.memory.session_capture import ingest_claude_code_history

    p = tmp_path / "history.jsonl"
    _history(p, _default_rows())

    kw = dict(
        composition=isolated_composition, path=str(p),
        principal_id="ben@example.org",
    )
    assert ingest_claude_code_history(**kw)["written"] == 1
    assert ingest_claude_code_history(**kw)["written"] == 0


def test_prompt_already_captured_in_full_is_not_duplicated(
    tmp_path: Path, isolated_composition,
):
    """The overlap window: don't re-add a half turn we already have whole."""
    from axiom.memory.session_capture import (
        ingest_claude_code_history,
        record_session_turn,
    )

    record_session_turn(
        composition=isolated_composition,
        principal_id="ben@example.org",
        tool="claude-code", model="claude-opus-5",
        user_input=_SUBSTANTIVE,
        assistant_output="Switched the object tier to SeaweedFS.",
        extra={"source_uuid": "real-1", "session_id": "s-1"},
    )

    p = tmp_path / "history.jsonl"
    _history(p, _default_rows())

    report = ingest_claude_code_history(
        composition=isolated_composition, path=str(p),
        principal_id="ben@example.org",
    )
    assert report["written"] == 0
    assert report["already_captured"] == 1


def test_history_is_registered_as_a_known_tool():
    from axiom.memory.session_capture import KNOWN_TOOL_PARSERS

    assert "claude-code-history" in KNOWN_TOOL_PARSERS


def test_default_history_path_is_discoverable():
    from axiom.memory.session_capture import default_history_path

    assert str(default_history_path(home=Path("/home/x"))).endswith(
        ".claude/history.jsonl"
    )


def test_dry_run_classifies_instead_of_reporting_zeros(
    tmp_path: Path, isolated_composition,
):
    """A dry run that always says 0 teaches the caller nothing."""
    from axiom.memory.session_capture import ingest_claude_code_history

    p = tmp_path / "history.jsonl"
    _history(p, _default_rows())

    report = ingest_claude_code_history(
        composition=isolated_composition, path=str(p),
        principal_id="ben@example.org", dry_run=True,
    )
    assert report["scanned"] == 2
    assert report["written"] == 1     # the substantive prompt
    assert report["filtered"] == 1    # "pwd"
    assert report["dry_run"] is True


# ---------------------------------------------------------------------------
# 2026-08-20 audit findings. Both are content loss, the failure mode this
# whole effort exists to stop, and both were mine.
# ---------------------------------------------------------------------------


def test_pasted_bodies_are_recovered_not_just_the_placeholder():
    """`display` says "[Pasted text #1 +374 lines]"; the body is elsewhere.

    Reading only `display` threw away 241,714 characters of real pasted
    context across 483 entries in the live history file.
    """
    import json as _json
    import tempfile
    from pathlib import Path as _Path

    from axiom.memory.session_capture import parse_claude_code_history

    rec = {
        "display": "Here's the failing config: [Pasted text #1 +12 lines]",
        "project": "/repo", "sessionId": "s-1", "timestamp": 1765831102276,
        "pastedContents": {
            "1": {"id": 1, "type": "text",
                  "content": "DB_URL=postgresql://app_ro@127.0.0.1/db\nPORT=8767"},
        },
    }
    with tempfile.TemporaryDirectory() as d:
        p = _Path(d) / "history.jsonl"
        p.write_text(_json.dumps(rec) + "\n")
        turn = parse_claude_code_history(str(p))[0]

    assert "Here's the failing config" in turn["user_input"]
    assert "DB_URL=postgresql" in turn["user_input"], "pasted body was dropped"
    assert "PORT=8767" in turn["user_input"]


def test_hash_only_pastes_do_not_break_parsing():
    """Older entries kept only a contentHash. Nothing to recover; don't crash."""
    import json as _json
    import tempfile
    from pathlib import Path as _Path

    from axiom.memory.session_capture import parse_claude_code_history

    rec = {
        "display": "[Pasted text #1 +374 lines]", "project": "/repo",
        "sessionId": "s-1", "timestamp": 1765831102276,
        "pastedContents": {"1": {"id": 1, "type": "text", "contentHash": "abc123"}},
    }
    with tempfile.TemporaryDirectory() as d:
        p = _Path(d) / "history.jsonl"
        p.write_text(_json.dumps(rec) + "\n")
        turns = parse_claude_code_history(str(p))

    assert len(turns) == 1
    assert turns[0]["user_input"].startswith("[Pasted text #1")


def test_source_uuid_survives_learning_to_read_more_of_a_record():
    """Identity keys on the record, not on the text we assemble from it.

    When pastedContents parsing was added, hashing the enriched prompt
    would have minted new ids for 431 already-captured turns and stored
    each of them twice.
    """
    import json as _json
    import tempfile
    from pathlib import Path as _Path

    from axiom.memory.session_capture import parse_claude_code_history

    base = {"display": "Here's the config: [Pasted text #1 +2 lines]",
            "project": "/repo", "sessionId": "s-1", "timestamp": 1765831102276}
    thin = dict(base)
    rich = dict(base, pastedContents={"1": {"id": 1, "type": "text",
                                            "content": "PORT=8767"}})

    ids = []
    for rec in (thin, rich):
        with tempfile.TemporaryDirectory() as d:
            p = _Path(d) / "h.jsonl"
            p.write_text(_json.dumps(rec) + "\n")
            ids.append(parse_claude_code_history(str(p))[0]["user_uuid"])

    assert ids[0] == ids[1], "enriching content must not change identity"


def test_reset_lets_an_improved_parse_replace_a_thinner_one(
    tmp_path: Path, isolated_composition,
):
    """Stable ids must not freeze in the worst read of a record."""
    from axiom.memory.session_capture import ingest_claude_code_history
    from axiom.memory.session_summary import list_fragments_by_principal

    thin = {"display": "Here's the config: [Pasted text #1 +1 lines]",
            "project": "/repo", "sessionId": "s-1", "timestamp": 1765831102276}
    p = tmp_path / "history.jsonl"
    _history(p, [thin])
    kw = dict(composition=isolated_composition, path=str(p),
              principal_id="ben@example.org")
    assert ingest_claude_code_history(**kw)["written"] == 1

    # Same record, but now we can read the pasted body.
    _history(p, [dict(thin, pastedContents={"1": {"id": 1, "type": "text",
                                                  "content": "PORT=8767"}})])
    assert ingest_claude_code_history(**kw)["written"] == 0      # stable id
    again = ingest_claude_code_history(**kw, reset=True)
    assert again["purged"] == 1
    assert again["written"] == 1

    frags = list_fragments_by_principal(
        isolated_composition, "ben@example.org", limit=10)
    live = [f for f in frags if "PORT=8767" in (f.content.get("user_input") or "")]
    assert len(live) == 1, "reset should replace, not duplicate"
