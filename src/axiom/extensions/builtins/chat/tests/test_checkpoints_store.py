# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Every agent file write leaves a pre-image and an action-ledger record that points at it (C15/C16).

The ledger is the log: the record's ``undo_ref`` names the checkpoint and its metadata carries the hashes
``/diff`` and ``/undo`` read back. These tests drive the real ``write_file`` tool against real files.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat

import pytest

from axiom.extensions.builtins.chat import checkpoints
from axiom.extensions.builtins.chat.tests._cmd_helpers import _jsonl_ledger  # noqa: F401
from axiom.extensions.builtins.chat.tools import execute_tool
from axiom.infra.paths import get_user_state_dir
from axiom.policy.action_ledger import ActionLedger


def _write(path, content):
    return execute_tool("write_file", {"file_path": str(path), "content": content})


def _rows(session_id):
    return [
        r
        for r in reversed(ActionLedger(state_dir=get_user_state_dir()).query(limit=1000))
        if (r.get("undo_ref") or "").startswith(f"checkpoint:{session_id}/")
    ]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_a_write_to_an_existing_file_keeps_the_pre_image_and_ledgers_it(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("before", encoding="utf-8")
    with checkpoints.turn_binding("sess1"):
        result = _write(target, "after")
    assert target.read_text() == "after" and "error" not in result
    (row,) = _rows("sess1")
    assert row["op_class"] == "chat.file.write" and row["outcome"] == "proceeded"
    assert row["candidate"] == str(target)
    md = row["metadata"]
    assert md["pre"] == _sha("before") and md["post"] == _sha("after") and md["kind"] == "write"
    blob = get_user_state_dir() / "checkpoints" / "sess1" / _sha("before")
    assert blob.read_text() == "before", "pre-image is stored under its content hash"


def test_a_new_file_is_recorded_as_did_not_exist(tmp_path):
    target = tmp_path / "sub" / "new.txt"
    with checkpoints.turn_binding("sess2"):
        _write(target, "hello")
    (row,) = _rows("sess2")
    assert row["metadata"]["pre"] is None and row["metadata"]["post"] == _sha("hello")


def test_writes_in_one_turn_share_a_turn_and_the_next_turn_gets_another(tmp_path):
    with checkpoints.turn_binding("sess3"):
        _write(tmp_path / "x", "1")
        _write(tmp_path / "y", "2")
    with checkpoints.turn_binding("sess3"):
        _write(tmp_path / "x", "3")
    a, b, c = _rows("sess3")
    assert a["metadata"]["turn"] == b["metadata"]["turn"] != c["metadata"]["turn"]
    assert [t.number for t in checkpoints.history("sess3")] == [1, 2]


def test_the_undo_ref_resolves_to_its_own_entry(tmp_path):
    with checkpoints.turn_binding("sess4"):
        _write(tmp_path / "x", "1")
    (row,) = _rows("sess4")
    assert row["undo_ref"].startswith("checkpoint:sess4/")
    (turn,) = checkpoints.history("sess4")
    assert turn.entries[0].undo_ref == row["undo_ref"]


def test_snapshots_are_owner_only(tmp_path):
    target = tmp_path / "s"
    target.write_text("old")
    with checkpoints.turn_binding("sess5"):
        _write(target, "new")
    root = get_user_state_dir() / "checkpoints" / "sess5"
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(root / _sha("old")).st_mode) == 0o600


def test_without_a_chat_turn_the_write_is_unchanged_and_nothing_is_snapshotted(tmp_path):
    result = _write(tmp_path / "plain", "x")
    assert (tmp_path / "plain").read_text() == "x" and "error" not in result
    assert not (get_user_state_dir() / "checkpoints").exists()


def test_a_pre_image_over_the_cap_refuses_the_write_and_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoints, "MAX_FILE_BYTES", 10)
    target = tmp_path / "big"
    target.write_text("x" * 50)
    with checkpoints.turn_binding("sess6"):
        result = _write(target, "small")
    assert target.read_text() == "x" * 50, "the file must not be touched when it cannot be snapshotted"
    assert "error" in result and "10 bytes" in result["error"] and "not written" in result["error"]
    assert _rows("sess6") == []


def test_new_content_over_the_cap_is_refused_too(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoints, "MAX_FILE_BYTES", 10)
    with checkpoints.turn_binding("sess6b"):
        result = _write(tmp_path / "n", "y" * 50)
    assert "error" in result and not (tmp_path / "n").exists()


def test_the_per_session_store_cap_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoints, "MAX_SESSION_BYTES", 30)
    with checkpoints.turn_binding("sess7"):
        assert "error" not in _write(tmp_path / "a", "a" * 10)  # post blob: 10
        assert "error" not in _write(tmp_path / "a", "b" * 10)  # pre 10 (dedup) + post 10 = 20
        r = _write(tmp_path / "a", "c" * 15)  # would reach 35 > 30
    assert "error" in r and "30 bytes" in r["error"]
    assert (tmp_path / "a").read_text() == "b" * 10


def test_a_secret_in_a_snapshot_is_flagged_by_pattern_name_and_never_ledgered(tmp_path):
    token = "ghp_" + "a1B2c3D4e5" * 4
    target = tmp_path / "env"
    target.write_text(f"TOKEN={token}\n")
    with checkpoints.turn_binding("sess8"):
        result = _write(target, "TOKEN=redacted\n")
    (row,) = _rows("sess8")
    assert row["metadata"]["secrets"] == ["github-token"]
    assert token not in json.dumps(row) and token not in json.dumps(result)
    assert "owner-only" in result["checkpoint_note"]


def test_a_write_that_fails_after_the_snapshot_is_ledgered_as_failed(tmp_path):
    target = tmp_path / "ro.txt"
    target.write_text("keep")
    target.chmod(0o444)
    try:
        with checkpoints.turn_binding("sess9"):
            with pytest.raises(OSError):
                _write(target, "x")
    finally:
        target.chmod(0o644)
    (row,) = _rows("sess9")
    assert row["outcome"] == "failed" and target.read_text() == "keep"
    assert [t for t in checkpoints.history("sess9")] == [], "a failed write is not an undoable turn"


def test_write_inbox_note_is_checkpointed_too(tmp_path, monkeypatch):
    from axiom.extensions.builtins.signals import cli as signals_cli

    monkeypatch.setattr(signals_cli, "INBOX_RAW", tmp_path / "inbox")
    with checkpoints.turn_binding("sess10"):
        out = execute_tool("write_inbox_note", {"text": "remember"})
    assert "path" in out
    (row,) = _rows("sess10")
    assert row["candidate"] == out["path"] and row["metadata"]["pre"] is None


@pytest.mark.parametrize("bad", ["", "../x", "a/b"])
def test_a_session_id_cannot_escape_the_checkpoint_root(bad):
    with pytest.raises(ValueError):
        with checkpoints.turn_binding(bad):
            pass
