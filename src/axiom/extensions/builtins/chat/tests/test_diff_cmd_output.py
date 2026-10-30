# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""/diff shows what the agent changed this session against its pre-images, with no repository involved."""

from __future__ import annotations

from axiom.extensions.builtins.chat.tests._cmd_helpers import (
    _jsonl_ledger,  # noqa: F401
    agent_turn,
    make_agent,
    run,
)


def test_a_unified_diff_grouped_by_turn_outside_any_repository(tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("one\ntwo\n")
    agent, _ = make_agent()
    sid = agent.session.session_id
    agent_turn(sid, [(a, "one\nTWO\n")])
    agent_turn(sid, [(tmp_path / "new.txt", "fresh\n")])
    out = run("/diff", agent)
    assert "Turn 1" in out and "Turn 2" in out
    assert out.index("Turn 1") < out.index("Turn 2")
    assert "-two" in out and "+TWO" in out
    assert "(did not exist)" in out and "+fresh" in out


def test_a_path_argument_limits_the_diff_to_that_file(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    agent, _ = make_agent()
    agent_turn(agent.session.session_id, [(a, "AAA\n"), (b, "BBB\n")])
    out = run(f"/diff {a}", agent)
    assert "+AAA" in out and "BBB" not in out


def test_no_changes_is_said_plainly():
    agent, _ = make_agent()
    out = run("/diff", agent)
    assert "no file changes" in out.lower()


def test_output_is_bounded_with_a_stated_limit_and_a_way_to_see_the_rest(tmp_path):
    big = tmp_path / "big.txt"
    agent, _ = make_agent()
    agent_turn(agent.session.session_id, [(big, "".join(f"line {i}\n" for i in range(500)))])
    out = run("/diff", agent)
    assert "line 100" in out and "line 499" not in out
    assert f"{checkpoints_limit()} lines" in out and "--full" in out and "--turn" in out
    full = run("/diff --full", agent)
    assert "line 499" in full and "--full" not in full


def checkpoints_limit():
    from axiom.extensions.builtins.chat.commands_ext import diff_cmd

    return diff_cmd.DIFF_MAX_LINES


def test_turn_selects_one_turn(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    agent, _ = make_agent()
    sid = agent.session.session_id
    agent_turn(sid, [(a, "first\n")])
    agent_turn(sid, [(b, "second\n")])
    out = run("/diff --turn 2", agent)
    assert "second" in out and "first" not in out
    assert "no turn 9" in run("/diff --turn 9", agent)


def test_a_file_edited_after_the_agent_wrote_it_is_flagged(tmp_path):
    a = tmp_path / "a"
    agent, _ = make_agent()
    agent_turn(agent.session.session_id, [(a, "agent\n")])
    a.write_text("agent\nuser added\n")
    out = run("/diff", agent)
    assert "+agent" in out and "changed since" in out
    assert "user added" not in out, "the diff shows what the agent wrote, not the user's later edit"


def test_a_credential_line_is_withheld_from_the_diff(tmp_path):
    token = "ghp_" + "a1B2c3D4e5" * 4
    a = tmp_path / "env"
    agent, _ = make_agent()
    agent_turn(agent.session.session_id, [(a, f"TOKEN={token}\nplain=1\n")])
    out = run("/diff", agent)
    assert token not in out and "withheld" in out and "github-token" in out and "+plain=1" in out


def test_binary_content_is_summarised_not_dumped(tmp_path):
    a = tmp_path / "bin"
    a.write_bytes(b"\xff\xfe\x00\x01")
    agent, _ = make_agent()
    agent_turn(agent.session.session_id, [(a, "now text\n")])
    out = run("/diff", agent)
    assert "binary" in out.lower()
