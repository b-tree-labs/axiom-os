# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""/undo restores a turn's files from pre-images, refuses to eat a later user edit, and is itself audited."""

from __future__ import annotations

import re

from axiom.extensions.builtins.chat import checkpoints
from axiom.extensions.builtins.chat.tests._cmd_helpers import (
    _jsonl_ledger,  # noqa: F401
    agent_turn,
    make_agent,
    run,
)
from axiom.infra.paths import get_user_state_dir
from axiom.policy.action_ledger import ActionLedger


def _setup(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("a0")
    b.write_text("b0")
    agent, _ = make_agent()
    sid = agent.session.session_id
    agent_turn(sid, [(a, "a1"), (b, "b1")])  # turn 1
    agent_turn(sid, [(a, "a2")])  # turn 2
    return agent, sid, a, b


def test_undo_reverts_only_the_last_turn(tmp_path):
    agent, _, a, b = _setup(tmp_path)
    out = run("/undo", agent)
    assert a.read_text() == "a1" and b.read_text() == "b1"
    assert "turn 2" in out and str(a) in out


def test_undo_n_reverts_n_turns_back_to_the_earliest_pre_image(tmp_path):
    agent, _, a, b = _setup(tmp_path)
    run("/undo 2", agent)
    assert a.read_text() == "a0" and b.read_text() == "b0"


def test_undo_turn_reverts_a_chosen_turn(tmp_path):
    agent, _, a, b = _setup(tmp_path)
    out = run("/undo --turn 1", agent)
    # turn 2 rewrote a.txt after turn 1: reverting turn 1 would lose that, so it must be refused
    assert a.read_text() == "a2" and b.read_text() == "b1"
    assert "changed since" in out and str(a) in out


def test_a_file_the_agent_created_is_removed_by_undo(tmp_path):
    agent, _ = make_agent()
    new = tmp_path / "made.txt"
    agent_turn(agent.session.session_id, [(new, "x")])
    out = run("/undo", agent)
    assert not new.exists() and "removed" in out


def test_a_later_user_edit_blocks_the_undo_and_is_shown(tmp_path):
    agent, _, a, _ = _setup(tmp_path)
    a.write_text("a2 plus my own edit")
    out = run("/undo", agent)
    assert a.read_text() == "a2 plus my own edit", "the user's edit must survive a refused undo"
    assert "changed since the agent wrote it" in out and str(a) in out and "--force" in out
    assert [t.reverted for t in checkpoints.history(agent.session.session_id)] == [False, False]


def test_force_overrides_and_the_overwritten_edit_stays_recoverable(tmp_path):
    agent, _, a, _ = _setup(tmp_path)
    a.write_text("a2 plus my own edit")
    plan = run("/undo --force", agent)
    # the destructive form is plan / apply: the first call changes nothing (see test_undo_force_plan_apply)
    assert a.read_text() == "a2 plus my own edit" and "Planned, not run" in plan
    held = re.search(r"held as (\S+),", plan).group(1)
    out = run(f"/undo --force --apply {held}", agent)
    assert a.read_text() == "a1"
    assert "forced" in out.lower()
    run("/undo", agent)  # undoing the undo restores what was on disk before it, my edit included
    assert a.read_text() == "a2 plus my own edit"


def test_an_undo_is_an_audited_ledger_action_with_its_own_undo_ref(tmp_path):
    agent, sid, a, _ = _setup(tmp_path)
    run("/undo", agent)
    rows = ActionLedger(state_dir=get_user_state_dir()).query(op_class="chat.file.undo", limit=10)
    (row,) = rows
    assert row["candidate"] == str(a) and row["outcome"] == "proceeded"
    assert row["undo_ref"].startswith(f"checkpoint:{sid}/")
    assert row["metadata"]["reverts"], "the record says which turn it reverted"


def test_an_undo_can_be_undone_once_and_the_redo_is_final(tmp_path):
    agent, sid, a, _ = _setup(tmp_path)
    run("/undo", agent)  # a2 -> a1
    assert a.read_text() == "a1"
    run("/undo", agent)  # undoes the undo: a1 -> a2
    assert a.read_text() == "a2"
    turns = checkpoints.history(sid)
    assert [t.kind for t in turns] == ["write", "write", "undo", "redo"]
    assert all(t.kind != "redo" for t in checkpoints.undoable(turns)), "a redo is never itself undone"


def test_after_an_undo_the_next_undo_goes_further_back(tmp_path):
    agent, _, a, b = _setup(tmp_path)
    run("/undo", agent)  # turn 2 reverted
    # next candidate is the undo itself (newest, undoable once) -> redo; then turn 2 is live again
    run("/undo", agent)
    assert a.read_text() == "a2"


def test_nothing_to_undo_says_so_and_states_the_limits(tmp_path):
    agent, _ = make_agent()
    out = run("/undo", agent)
    assert "nothing to undo" in out.lower()
    assert "shell" in out and "network" in out


def test_success_output_states_what_cannot_be_undone(tmp_path):
    agent, *_ = _setup(tmp_path)
    out = run("/undo", agent)
    assert checkpoints.CANNOT_UNDO in out


def test_a_missing_snapshot_refuses_and_changes_nothing(tmp_path):
    agent, sid, a, b = _setup(tmp_path)
    root = get_user_state_dir() / "checkpoints" / sid
    import hashlib

    (root / hashlib.sha256(b"a1").hexdigest()).unlink()
    out = run("/undo", agent)
    assert a.read_text() == "a2" and "missing" in out


def test_a_bad_argument_is_a_usage_message(tmp_path):
    agent, *_ = _setup(tmp_path)
    assert "Usage" in run("/undo banana", agent)
    assert "Usage" in run("/undo --turn x", agent)
    assert "no turn 99" in run("/undo --turn 99", agent)
