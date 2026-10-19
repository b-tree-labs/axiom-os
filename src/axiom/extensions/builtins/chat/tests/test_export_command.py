# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""/export writes the transcript, scanned whole for secrets, never overwriting (ADR-168, C7)."""

from __future__ import annotations

import json

from axiom.extensions.builtins.chat.tests._transcript_support import (
    FAKE_GH_TOKEN,
    FAKE_OPENAI_KEY,
    make_agent,
    run,
)
from axiom.infra.orchestrator.session import Message

TURNS = [("what is x", "x is y"), ("and z", "z is w")]


def _written(out: str, tmp_path):
    files = [p for p in tmp_path.rglob("*") if p.is_file() and p.suffix in (".md", ".json")]
    return files


def test_default_path_is_under_the_state_dir_exports(tmp_path, monkeypatch):
    agent = make_agent(monkeypatch, TURNS)
    out = run(agent, "/export")
    exports = tmp_path / "axi-state" / "exports"
    files = list(exports.glob(f"{agent.session.session_id}-*.md"))
    assert len(files) == 1
    assert str(files[0]) in out
    body = files[0].read_text()
    assert "x is y" in body and "z is w" in body and "what is x" in body


def test_markdown_header_carries_the_where_stamp_and_says_per_reply_stamps_are_unrecorded(
    tmp_path, monkeypatch
):
    dest = tmp_path / "t.md"
    run(make_agent(monkeypatch, TURNS), f"/export {dest}")
    body = dest.read_text()
    assert "RAG: LOCAL" in body
    assert "per-reply stamps are not recorded" in body


def test_it_never_overwrites_an_existing_file(tmp_path, monkeypatch):
    dest = tmp_path / "t.md"
    dest.write_text("precious")
    out = run(make_agent(monkeypatch, TURNS), f"/export {dest}")
    assert dest.read_text() == "precious"
    assert "exists" in out


def test_two_default_exports_in_the_same_second_do_not_collide(tmp_path, monkeypatch):
    agent = make_agent(monkeypatch, TURNS)
    run(agent, "/export")
    run(agent, "/export")
    assert len(list((tmp_path / "axi-state" / "exports").glob("*.md"))) == 2


def test_a_secret_anywhere_in_the_transcript_refuses_and_writes_nothing(tmp_path, monkeypatch):
    agent = make_agent(monkeypatch, [("my key is " + FAKE_OPENAI_KEY, "noted")])
    dest = tmp_path / "t.md"
    out = run(agent, f"/export {dest}")
    assert not dest.exists()
    assert "openai_key" in out and "--redact" in out
    assert FAKE_OPENAI_KEY not in out


def test_redact_replaces_matches_and_records_how_many(tmp_path, monkeypatch):
    agent = make_agent(
        monkeypatch, [("key " + FAKE_OPENAI_KEY, f"and {FAKE_OPENAI_KEY} plus {FAKE_GH_TOKEN}")]
    )
    dest = tmp_path / "t.md"
    out = run(agent, f"/export {dest} --redact")
    body = dest.read_text()
    assert FAKE_OPENAI_KEY not in body and FAKE_GH_TOKEN not in body
    assert "[redacted:openai_key]" in body and "[redacted:github_token]" in body
    # The session title is auto-derived from the first message, so that match is counted too: 1 title +
    # 1 user message + 2 in the reply.
    assert "4 redactions" in body and "4 redactions" in out
    # The session itself is not altered by an export.
    assert FAKE_OPENAI_KEY in agent.session.messages[0].content


def test_a_secret_in_a_tool_call_argument_is_caught_in_json(tmp_path, monkeypatch):
    agent = make_agent(monkeypatch, TURNS)
    agent.session.add_message("assistant", "ran", tool_calls=[{"name": "t", "args": {"k": FAKE_GH_TOKEN}}])
    dest = tmp_path / "t.json"
    out = run(agent, f"/export {dest}")
    assert not dest.exists() and "github_token" in out


def test_json_round_trips_through_the_session_message_schema(tmp_path, monkeypatch):
    agent = make_agent(monkeypatch, TURNS)
    agent.session.add_message("assistant", "ran", tool_calls=[{"name": "t", "args": {"a": 1}}])
    dest = tmp_path / "t.json"
    run(agent, f"/export {dest} --format json")
    doc = json.loads(dest.read_text())
    assert doc["schema"] == "axiom.chat.export/1"
    assert doc["session"]["session_id"] == agent.session.session_id
    assert doc["where"]["provider"] == "home" and doc["stamps_recorded"] is False
    back = [Message.from_dict(m) for m in doc["messages"]]
    assert [m.to_dict() for m in back] == [m.to_dict() for m in agent.session.messages]


def test_format_is_inferred_from_the_extension_and_unknown_formats_are_refused(tmp_path, monkeypatch):
    agent = make_agent(monkeypatch, TURNS)
    run(agent, f"/export {tmp_path / 'a.json'}")
    assert json.loads((tmp_path / "a.json").read_text())["schema"]
    out = run(agent, f"/export {tmp_path / 'b.txt'} --format xml")
    assert "md" in out and "json" in out and not (tmp_path / "b.txt").exists()


def test_empty_transcript_is_not_exported(tmp_path, monkeypatch):
    out = run(make_agent(monkeypatch), f"/export {tmp_path / 't.md'}")
    assert "nothing to export" in out.lower() and not (tmp_path / "t.md").exists()


def test_overlapping_classes_count_once_and_other_key_shapes_are_caught():
    from axiom.extensions.builtins.chat.commands_ext import _transcript as tx

    text, counts = tx.redact_text(f"token: {FAKE_GH_TOKEN}")
    assert counts == {"github_token": 1} and text == "token: [redacted:github_token]"
    bearer = "Authorization: Bearer " + "abcdEFGH1234" * 3
    assert tx.scan(bearer) == {"bearer_token": 1}
    assert not tx.scan("an ordinary sentence about keys and tokens in general")
