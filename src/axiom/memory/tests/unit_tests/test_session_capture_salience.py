# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Turn aggregation + salience for cross-tool session capture.

Regression cover for the 2026-08-18 capture audit, which found 3584 of
4526 ledger fragments (79%) carrying an empty ``user_input`` *and* an
empty ``assistant_output``. Root cause: the transcript parser paired
every ``type=user`` record with the next ``type=assistant`` record and
kept only ``type=="text"`` content blocks — so each hop of a tool-use
loop (user=tool_result, assistant=tool_use) landed as a hollow shell.
Those shells then poisoned the ``source_uuid`` dedupe, permanently
blocking the real content from ever being written.

The fix is two-part and both parts are asserted here:

1. **Aggregate, don't pair.** One logical turn = one real user prompt
   plus every assistant message up to the next real user prompt. Tool
   traffic becomes provenance (``extra.tools_used``), not a fragment.
2. **Salience gate.** A turn is written only if it carries something
   worth recovering later. Noise ("ok", "continue", bare slash
   commands) is counted and dropped rather than stored.
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


def _tool_loop_transcript(path: Path) -> None:
    """A transcript shaped like real agentic Claude Code traffic.

    One substantive user prompt, then a tool loop: assistant emits a
    ``tool_use``, the harness replies with a ``tool_result`` on a
    ``type=user`` line, assistant emits another ``tool_use``, and only
    at the end does the assistant produce prose. The old pair-wise
    parser turned this into 3 fragments, 2 of them hollow.
    """
    lines = [
        {"type": "permission-mode", "permissionMode": "default", "sessionId": "s-1"},
        {
            "type": "user", "uuid": "u-1", "sessionId": "s-1",
            "timestamp": "2026-08-18T10:00:00.000Z", "cwd": "/repo",
            "gitBranch": "main", "version": "2.0.0",
            "message": {"role": "user", "content": "Why is the reactor flux estimate drifting?"},
        },
        {
            "type": "assistant", "uuid": "a-1", "sessionId": "s-1",
            "timestamp": "2026-08-18T10:00:05.000Z",
            "message": {
                "role": "assistant", "model": "claude-opus-5",
                "content": [
                    {"type": "text", "text": "Let me check the calibration table."},
                    {"type": "tool_use", "id": "t-1", "name": "Bash",
                     "input": {"command": "grep -n flux calib.py"}},
                ],
            },
        },
        # tool_result comes back on a type=user line — NOT a real user turn
        {
            "type": "user", "uuid": "u-2", "sessionId": "s-1",
            "timestamp": "2026-08-18T10:00:06.000Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t-1",
                     "content": "calib.py:42: flux_scale = 1.0"},
                ],
            },
        },
        {
            "type": "assistant", "uuid": "a-2", "sessionId": "s-1",
            "timestamp": "2026-08-18T10:00:09.000Z",
            "message": {
                "role": "assistant", "model": "claude-opus-5",
                "content": [
                    {"type": "tool_use", "id": "t-2", "name": "Read",
                     "input": {"file_path": "/repo/calib.py"}},
                ],
            },
        },
        {
            "type": "user", "uuid": "u-3", "sessionId": "s-1",
            "timestamp": "2026-08-18T10:00:10.000Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t-2", "content": "..."},
                ],
            },
        },
        {
            "type": "assistant", "uuid": "a-3", "sessionId": "s-1",
            "timestamp": "2026-08-18T10:00:15.000Z",
            "message": {
                "role": "assistant", "model": "claude-opus-5",
                "content": [
                    {"type": "text",
                     "text": "flux_scale is pinned at 1.0, so the drift is "
                             "the uncorrected detector gain, not the model."},
                ],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


# ---------------------------------------------------------------------------
# 1. Turn aggregation — the hollow-fragment root cause
# ---------------------------------------------------------------------------


def test_tool_loop_collapses_to_one_substantive_turn(tmp_path: Path):
    """A prompt + tool loop + answer is ONE turn, with no hollow shells."""
    from axiom.memory.session_capture import parse_claude_code_jsonl

    p = tmp_path / "s.jsonl"
    _tool_loop_transcript(p)

    pairs = parse_claude_code_jsonl(str(p))

    assert len(pairs) == 1, f"expected 1 aggregated turn, got {len(pairs)}"
    turn = pairs[0]
    assert turn["user_input"] == "Why is the reactor flux estimate drifting?"
    # Assistant side carries prose from across the whole loop.
    assert "calibration table" in turn["assistant_output"]
    assert "uncorrected detector gain" in turn["assistant_output"]
    # Tool traffic is provenance, not content.
    assert turn["tools_used"] == ["Bash", "Read"]
    assert turn["model"] == "claude-opus-5"
    assert turn["user_uuid"] == "u-1"


def test_no_fragment_is_hollow(tmp_path: Path):
    """Every parsed turn has at least one non-empty side. The 79% bug."""
    from axiom.memory.session_capture import parse_claude_code_jsonl

    p = tmp_path / "s.jsonl"
    _tool_loop_transcript(p)

    for turn in parse_claude_code_jsonl(str(p)):
        assert (turn["user_input"].strip() or turn["assistant_output"].strip()), (
            f"hollow turn emitted: {turn!r}"
        )


def test_tool_result_lines_never_become_user_turns(tmp_path: Path):
    """tool_result payloads must not be mistaken for the user speaking."""
    from axiom.memory.session_capture import parse_claude_code_jsonl

    p = tmp_path / "s.jsonl"
    _tool_loop_transcript(p)

    for turn in parse_claude_code_jsonl(str(p)):
        assert "calib.py:42" not in turn["user_input"]


def test_sidechain_records_are_skipped(tmp_path: Path):
    """Subagent (sidechain) traffic is not the user's conversation."""
    from axiom.memory.session_capture import parse_claude_code_jsonl

    p = tmp_path / "s.jsonl"
    lines = [
        {
            "type": "user", "uuid": "u-1", "sessionId": "s-1", "isSidechain": True,
            "timestamp": "2026-08-18T10:00:00.000Z",
            "message": {"role": "user", "content": "subagent instruction text"},
        },
        {
            "type": "assistant", "uuid": "a-1", "sessionId": "s-1", "isSidechain": True,
            "timestamp": "2026-08-18T10:00:01.000Z",
            "message": {"role": "assistant", "model": "m",
                        "content": [{"type": "text", "text": "subagent reply"}]},
        },
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")

    assert parse_claude_code_jsonl(str(p)) == []


# ---------------------------------------------------------------------------
# 2. Salience — record valuable points, not raw dumps
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "user_input",
    ["ok", "thanks", "continue", "yes", "y", "go on", "  ", "/clear", "proceed"],
)
def test_noise_turns_are_not_salient(user_input: str):
    from axiom.memory.salience import score_turn

    verdict = score_turn(user_input=user_input, assistant_output="Done.")
    assert not verdict.salient, f"{user_input!r} should be filtered as noise"


def test_decision_bearing_turn_is_salient():
    from axiom.memory.salience import score_turn

    verdict = score_turn(
        user_input="Use SeaweedFS instead of MinIO for the object tier.",
        assistant_output="Switched the storage backend to SeaweedFS.",
    )
    assert verdict.salient
    assert "decision" in verdict.reasons


def test_substantive_technical_turn_is_salient():
    from axiom.memory.salience import score_turn

    verdict = score_turn(
        user_input="Why is the reactor flux estimate drifting?",
        assistant_output=(
            "flux_scale is pinned at 1.0, so the drift is the uncorrected "
            "detector gain rather than the model itself."
        ),
    )
    assert verdict.salient


def test_hollow_turn_is_never_salient():
    from axiom.memory.salience import score_turn

    assert not score_turn(user_input="", assistant_output="").salient


def test_summary_is_not_a_truncated_dump():
    """The summary should read as a point, not the first 80 raw chars."""
    from axiom.memory.salience import summarize_turn

    summary = summarize_turn(
        user_input="Use SeaweedFS instead of MinIO for the object tier.",
        assistant_output="Switched the storage backend to SeaweedFS.",
        tools_used=["Edit"],
    )
    assert "SeaweedFS" in summary
    assert len(summary) <= 240


# ---------------------------------------------------------------------------
# 3. Ingest end-to-end — salience gate is enforced on the write path
# ---------------------------------------------------------------------------


def test_ingest_reports_filtered_and_writes_only_salient(
    tmp_path: Path, isolated_composition,
):
    from axiom.memory.session_capture import ingest_claude_code_jsonl

    p = tmp_path / "s.jsonl"
    lines = [
        # salient
        {
            "type": "user", "uuid": "u-1", "sessionId": "s-1",
            "timestamp": "2026-08-18T10:00:00.000Z",
            "message": {"role": "user",
                        "content": "Always use SeaweedFS, never MinIO."},
        },
        {
            "type": "assistant", "uuid": "a-1", "sessionId": "s-1",
            "timestamp": "2026-08-18T10:00:01.000Z",
            "message": {"role": "assistant", "model": "claude-opus-5",
                        "content": [{"type": "text", "text": "Understood — SeaweedFS it is."}]},
        },
        # noise
        {
            "type": "user", "uuid": "u-2", "sessionId": "s-1",
            "timestamp": "2026-08-18T10:01:00.000Z",
            "message": {"role": "user", "content": "ok"},
        },
        {
            "type": "assistant", "uuid": "a-2", "sessionId": "s-1",
            "timestamp": "2026-08-18T10:01:01.000Z",
            "message": {"role": "assistant", "model": "claude-opus-5",
                        "content": [{"type": "text", "text": "👍"}]},
        },
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")

    report = ingest_claude_code_jsonl(
        composition=isolated_composition, path=str(p),
        principal_id="ben@example.org",
    )

    assert report["written"] == 1
    assert report["filtered"] == 1
    assert report["scanned"] == 2


def test_hollow_existing_fragment_does_not_block_recapture(
    tmp_path: Path, isolated_composition,
):
    """The dedupe must not treat a hollow shell as 'already captured'.

    This is what made the 3584 empties permanent: once a uuid was in
    the ledger it was skipped forever, even though nothing was stored.
    """
    from axiom.memory.session_capture import (
        ingest_claude_code_jsonl,
        record_session_turn,
    )

    # Pre-seed a hollow fragment carrying the uuid we are about to ingest.
    record_session_turn(
        composition=isolated_composition,
        principal_id="ben@example.org",
        tool="claude-code",
        model="claude-opus-5",
        user_input="",
        assistant_output="",
        extra={"source_uuid": "u-1"},
    )

    p = tmp_path / "s.jsonl"
    lines = [
        {
            "type": "user", "uuid": "u-1", "sessionId": "s-1",
            "timestamp": "2026-08-18T10:00:00.000Z",
            "message": {"role": "user",
                        "content": "Always use SeaweedFS, never MinIO."},
        },
        {
            "type": "assistant", "uuid": "a-1", "sessionId": "s-1",
            "timestamp": "2026-08-18T10:00:01.000Z",
            "message": {"role": "assistant", "model": "claude-opus-5",
                        "content": [{"type": "text", "text": "Understood."}]},
        },
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")

    report = ingest_claude_code_jsonl(
        composition=isolated_composition, path=str(p),
        principal_id="ben@example.org",
    )
    assert report["written"] == 1, "hollow shell wrongly suppressed re-capture"


def test_reingest_of_real_content_is_still_a_noop(
    tmp_path: Path, isolated_composition,
):
    """Idempotency must survive the hollow-aware dedupe change."""
    from axiom.memory.session_capture import ingest_claude_code_jsonl

    p = tmp_path / "s.jsonl"
    lines = [
        {
            "type": "user", "uuid": "u-1", "sessionId": "s-1",
            "timestamp": "2026-08-18T10:00:00.000Z",
            "message": {"role": "user",
                        "content": "Always use SeaweedFS, never MinIO."},
        },
        {
            "type": "assistant", "uuid": "a-1", "sessionId": "s-1",
            "timestamp": "2026-08-18T10:00:01.000Z",
            "message": {"role": "assistant", "model": "claude-opus-5",
                        "content": [{"type": "text", "text": "Understood."}]},
        },
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")

    kw = dict(
        composition=isolated_composition, path=str(p),
        principal_id="ben@example.org",
    )
    assert ingest_claude_code_jsonl(**kw)["written"] == 1
    second = ingest_claude_code_jsonl(**kw)
    assert second["written"] == 0
    assert second["skipped"] == 1


# ---------------------------------------------------------------------------
# 4. In-flight turns — the 5-minute sweep catching a turn mid-answer
# ---------------------------------------------------------------------------


def _in_flight_transcript(path: Path) -> None:
    """A transcript sampled while the assistant is still working.

    The prompt is written and a tool_use has fired, but no prose has
    landed yet. Capturing this as a finished turn is destructive: the
    source_uuid dedupe then locks it, so the answer -- which arrives
    seconds later -- can never be added.
    """
    lines = [
        {
            "type": "user", "uuid": "u-1", "sessionId": "s-1",
            "timestamp": "2026-08-19T17:06:51.000Z", "cwd": "/repo",
            "message": {"role": "user",
                        "content": "Why did the ingest sweep write nothing this run?"},
        },
        {
            "type": "assistant", "uuid": "a-1", "sessionId": "s-1",
            "timestamp": "2026-08-19T17:06:52.000Z",
            "message": {
                "role": "assistant", "model": "claude-opus-5",
                "content": [{"type": "tool_use", "id": "t-1", "name": "Bash",
                             "input": {"command": "tail log"}}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def test_in_flight_turn_is_not_captured_as_finished(tmp_path: Path):
    """Don't freeze a half-answered turn; the next pass gets it whole."""
    from axiom.memory.session_capture import parse_claude_code_jsonl

    p = tmp_path / "s.jsonl"
    _in_flight_transcript(p)

    assert parse_claude_code_jsonl(str(p)) == []


def test_completed_turn_is_captured_once_the_answer_lands(tmp_path: Path):
    from axiom.memory.session_capture import parse_claude_code_jsonl

    p = tmp_path / "s.jsonl"
    _in_flight_transcript(p)
    with p.open("a") as f:
        f.write(json.dumps({
            "type": "assistant", "uuid": "a-2", "sessionId": "s-1",
            "timestamp": "2026-08-19T17:06:58.000Z",
            "message": {"role": "assistant", "model": "claude-opus-5",
                        "content": [{"type": "text",
                                     "text": "Every turn deduped against an existing uuid."}]},
        }) + "\n")

    turns = parse_claude_code_jsonl(str(p))
    assert len(turns) == 1
    assert "deduped against an existing uuid" in turns[0]["assistant_output"]
    assert turns[0]["user_uuid"] == "u-1"


def test_earlier_turns_still_captured_while_the_last_is_in_flight(tmp_path: Path):
    """Only the trailing turn is held back, never the completed ones."""
    from axiom.memory.session_capture import parse_claude_code_jsonl

    p = tmp_path / "s.jsonl"
    lines = [
        {"type": "user", "uuid": "u-1", "sessionId": "s-1",
         "timestamp": "2026-08-19T17:00:00.000Z",
         "message": {"role": "user", "content": "Always pin the service venv, never editable."}},
        {"type": "assistant", "uuid": "a-1", "sessionId": "s-1",
         "timestamp": "2026-08-19T17:00:05.000Z",
         "message": {"role": "assistant", "model": "m",
                     "content": [{"type": "text", "text": "Pinned to the non-editable install."}]}},
        # second turn still in flight
        {"type": "user", "uuid": "u-2", "sessionId": "s-1",
         "timestamp": "2026-08-19T17:06:51.000Z",
         "message": {"role": "user", "content": "And what about the Cursor sweep path?"}},
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")

    turns = parse_claude_code_jsonl(str(p))
    assert [t["user_uuid"] for t in turns] == ["u-1"]


def test_lossless_mode_can_still_take_the_tail(tmp_path: Path):
    """A deliberate archival dump may want the unanswered prompt."""
    from axiom.memory.session_capture import parse_claude_code_jsonl

    p = tmp_path / "s.jsonl"
    _in_flight_transcript(p)

    turns = parse_claude_code_jsonl(str(p), include_incomplete_tail=True)
    assert len(turns) == 1
    assert turns[0]["assistant_output"] == ""


# ---------------------------------------------------------------------------
# 2026-08-20 calibration audit: the gate dropped 24% of recovered prompts,
# and a real slice of those carried decisions, assignments and facts.
#
# The scoring was designed for a FULL exchange, where a substantive answer
# contributes most of the signal. Applied to a recovered prompt whose answer
# is permanently gone, it penalises exactly the terse, high-value statements
# worth keeping — "make this a system dependency" scores 1 and vanishes.
#
# For a prompt-only turn the prompt is all that survives, so the bar is
# "is this more than acknowledgement", not "does it clear a score built
# around an answer that no longer exists".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prompt", [
    "make this a system dependency",
    "This needs to go into our FAQ",
    "i will work with Ondrej and Soho on it.",
    "we're worse off in this half-step mode.",
    "the build host is available.",
])
def test_short_decisions_survive_prompt_only_capture(prompt: str):
    """Real false negatives observed in the live history backfill."""
    from axiom.memory.salience import score_turn

    verdict = score_turn(user_input=prompt, assistant_output="", prompt_only=True)
    assert verdict.salient, f"dropped a decision: {prompt!r}"


@pytest.mark.parametrize("prompt", [
    "ok", "merged", "/mcp", "/compact", "[Image #8]", "yes", "continue",
])
def test_glue_is_still_dropped_in_prompt_only_mode(prompt: str):
    """Loosening the bar must not turn the gate off."""
    from axiom.memory.salience import score_turn

    assert not score_turn(
        user_input=prompt, assistant_output="", prompt_only=True,
    ).salient


def test_full_exchange_scoring_is_unchanged():
    """The looser bar applies only where the answer is gone."""
    from axiom.memory.salience import score_turn

    # A terse prompt with a terse answer stays below the bar for full turns.
    assert not score_turn(
        user_input="do phase 2", assistant_output="Done.",
    ).salient
