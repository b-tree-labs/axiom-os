# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The session readers behind /renew's sources: real fixtures in each harness's on-disk format, read-only."""

from __future__ import annotations

import pytest

from axiom.memory.absorb.base import SessionCapable
from axiom.memory.absorb.sessions import (
    ClaudeCodeSessionReader,
    CodexSessionReader,
    session_readers,
)

from ._renew_harness_support import (
    CLAUDE_ID,
    CLAUDE_OLD_ID,
    CODEX_ARCHIVED_ID,
    CODEX_ID,
    SECRET,
    claude_session,
    codex_session,
    fingerprint,
)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def test_only_harnesses_with_a_verified_conversation_format_are_registered(home):
    # A reader ships only where the format was verified from a real install or real files (see the table in
    # axiom.memory.absorb.sessions). Letta, aider, Roo and Grok have none, and say why (session_status).
    assert sorted(session_readers(home)) == sorted(
        ["claude-code", "codex", "opencode", "hermes", "goose", "gemini", "pi", "cline", "continue", "cursor"]
    )
    for reader in session_readers(home).values():
        assert isinstance(reader, SessionCapable)


def test_claude_code_lists_newest_first_with_the_harness_title_and_counts_only_the_conversation(home):
    claude_session(home, CLAUDE_OLD_ID, first_prompt="older work", title="Older", hours_ago=30)
    claude_session(home, hours_ago=2)
    headers = ClaudeCodeSessionReader(home).list_sessions()
    assert [h.source_session_id for h in headers] == [CLAUDE_ID, CLAUDE_OLD_ID]
    newest = headers[0]
    assert newest.title == "Detector calibration plan" and newest.harness == "claude-code"
    # user, assistant (text+call+result+text merged), user, assistant: sidechain/meta/tool-result lines add none
    assert newest.message_count == 4
    assert newest.cwd == "/work/detector"


def test_claude_code_conversion_keeps_roles_and_turns_tool_traffic_into_text(home):
    claude_session(home)
    t = ClaudeCodeSessionReader(home).load_session(CLAUDE_ID)
    assert [m.role for m in t.messages] == ["user", "assistant", "user", "assistant"]
    first = t.messages[1].content
    assert "I will look at the files." in first
    assert "[tool call: Bash(command=ls -la)]" in first
    assert "[tool result: 120 chars, not imported]" in first
    assert "Calibration needs two reference runs." in first
    assert "x" * 20 not in first  # the result body is not copied
    assert t.messages[2].content == "Thanks, and the second run?"
    joined = "\n".join(m.content for m in t.messages)
    assert "SUBAGENT CHATTER" not in joined and "META NOISE" not in joined
    assert t.lossy == {"thinking_dropped": 1, "tool_calls": 1, "tool_results": 1}


def test_codex_lists_sessions_and_archived_ones_and_converts_the_rollout(home):
    codex_session(home, hours_ago=5)
    codex_session(home, CODEX_ARCHIVED_ID, archived=True, hours_ago=50, prompt="old flux question")
    reader = CodexSessionReader(home)
    assert [h.source_session_id for h in reader.list_sessions()] == [CODEX_ID, CODEX_ARCHIVED_ID]
    t = reader.load_session(CODEX_ID)
    assert [m.role for m in t.messages] == ["user", "assistant", "user", "assistant"]
    assert t.messages[0].content == "Check the flux log"  # the injected environment block is not the person
    assert "[tool call: shell(command=cat flux.log)]" in t.messages[1].content
    assert "[tool result: 50 chars, not imported]" in t.messages[1].content
    assert "The flux is flat." in t.messages[1].content
    assert "<permissions>" not in "".join(m.content for m in t.messages)
    assert t.lossy == {"harness_context_dropped": 1, "thinking_dropped": 1, "tool_calls": 1, "tool_results": 1}
    assert reader.load_session(CODEX_ARCHIVED_ID).messages[0].content == "old flux question"


def test_reading_never_touches_the_source(home):
    claude_session(home)
    codex_session(home)
    codex_session(home, CODEX_ARCHIVED_ID, archived=True)
    before = fingerprint(home)
    for reader in session_readers(home).values():
        reader.list_sessions()
    ClaudeCodeSessionReader(home).load_session(CLAUDE_ID)
    CodexSessionReader(home).load_session(CODEX_ID)
    assert fingerprint(home) == before  # same bytes, same mtimes, nothing created or removed


def test_a_secret_in_a_tool_argument_is_not_carried_into_the_summary(home):
    claude_session(home, extra_tool_arg=f"curl -H 'Authorization: {SECRET}' https://x")
    t = ClaudeCodeSessionReader(home).load_session(CLAUDE_ID)
    assert SECRET not in "\n".join(m.content for m in t.messages)
    assert "[tool call: Bash(command=(withheld))]" in t.messages[1].content


def test_an_unknown_or_hostile_id_finds_nothing(home):
    claude_session(home)
    reader = ClaudeCodeSessionReader(home)
    assert reader.load_session("nope") is None
    assert reader.load_session("../../etc/passwd") is None
    assert reader.load_session("") is None


def test_a_corrupt_file_is_skipped_and_recorded_not_fatal(home):
    claude_session(home)
    bad = home / ".claude" / "projects" / "-p" / "bad.jsonl"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"\xff\xfe not json at all\n{{{")
    reader = ClaudeCodeSessionReader(home)
    assert [h.source_session_id for h in reader.list_sessions()] == [CLAUDE_ID]


def test_a_machine_without_the_harness_lists_nothing(home):
    for reader in session_readers(home).values():
        assert reader.list_sessions() == []
