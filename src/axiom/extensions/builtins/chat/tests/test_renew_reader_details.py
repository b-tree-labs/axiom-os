# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Format semantics each new reader must respect (beyond the shared conformance properties), and what /renew
says about the harnesses that have no reader."""

from __future__ import annotations

import json
import sqlite3

import pytest

from axiom.extensions.builtins.chat.commands_ext import renew, renew_sources
from axiom.infra.orchestrator.session import Session
from axiom.memory.absorb.session_status import HARNESS_STATUS
from axiom.memory.absorb.sessions import session_readers

from ._renew_support import ME, Agent, new_store
from ._session_conformance_support import all_cases


@pytest.fixture
def home(_no_foreign_harness_state):
    return _no_foreign_harness_state


def case(name):
    return next(c for c in all_cases() if c.name == name)


def _append(path, *records):
    with open(path, "a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


# ---- Gemini: the CLI's own replay rules ---------------------------------------------------------


def test_gemini_a_record_seen_again_replaces_the_earlier_one_in_place(home):
    c = case("gemini")
    c.install(home)
    p = c.primary(home)
    first = next(
        r for r in map(json.loads, p.read_text().splitlines()) if r.get("type") == "gemini"
    )
    _append(p, {**first, "content": "REVISED"})
    t = session_readers(home)["gemini"].load_session(c.session_id)
    texts = "\n".join(m.content for m in t.messages)
    assert "REVISED" in texts and "Let me look." not in texts


def test_gemini_rewind_drops_that_message_and_everything_after(home):
    c = case("gemini")
    c.install(home)
    p = c.primary(home)
    recs = [json.loads(ln) for ln in p.read_text().splitlines()]
    user_ids = [r["id"] for r in recs if r.get("type") == "user" and "Run echo" in json.dumps(r)]
    _append(p, {"$rewindTo": user_ids[0]})
    assert session_readers(home)["gemini"].load_session(c.session_id) is None or not (
        session_readers(home)["gemini"].load_session(c.session_id).messages
    )


def test_gemini_set_messages_replaces_the_whole_list_and_summary_becomes_the_title(home):
    c = case("gemini")
    c.install(home)
    _append(
        c.primary(home),
        {
            "$set": {
                "summary": "A tidy summary",
                "messages": [
                    {
                        "id": "n1",
                        "type": "user",
                        "timestamp": "2026-10-09T00:00:00Z",
                        "content": [{"text": "fresh start"}],
                    },
                    {
                        "id": "n2",
                        "type": "gemini",
                        "timestamp": "2026-10-09T00:00:01Z",
                        "content": "fresh reply",
                    },
                ],
            }
        },
    )
    t = session_readers(home)["gemini"].load_session(c.session_id)
    assert [m.content for m in t.messages] == [
        "fresh start",
        "fresh reply",
    ] and t.header.title == "A tidy summary"


def test_gemini_session_context_and_tool_result_echoes_are_not_the_person_speaking(home):
    c = case("gemini")
    c.install(home)
    t = session_readers(home)["gemini"].load_session(c.session_id)
    assert [m.role for m in t.messages] == ["user", "assistant"]
    assert t.lossy["harness_context_dropped"] == 1


# ---- Pi: the tree --------------------------------------------------------------------------------


def test_pi_follows_the_branch_the_person_is_on_and_leaves_abandoned_branches_out(home):
    c = case("pi")
    c.install(home)
    p = c.primary(home)
    recs = [json.loads(ln) for ln in p.read_text().splitlines()]
    first_user = next(
        r for r in recs if r.get("type") == "message" and r["message"]["role"] == "user"
    )
    # an abandoned branch hangs off the model-change entry; the live one is added last, off the first user turn
    _append(
        p,
        {
            "type": "message",
            "id": "dead",
            "parentId": first_user["parentId"],
            "timestamp": "2026-10-09T00:00:00Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "ABANDONED BRANCH"}]},
        },
        {
            "type": "message",
            "id": "live",
            "parentId": first_user["id"],
            "timestamp": "2026-10-09T00:00:01Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "LIVE BRANCH"}]},
        },
    )
    t = session_readers(home)["pi"].load_session(c.session_id)
    joined = "\n".join(m.content for m in t.messages)
    assert [m.role for m in t.messages] == ["user", "assistant"] and t.messages[
        -1
    ].content == "LIVE BRANCH"
    assert "ABANDONED" not in joined


def test_pi_session_name_is_the_title(home):
    c = case("pi")
    c.install(home)
    p = c.primary(home)
    last = json.loads(p.read_text().splitlines()[-1])
    _append(
        p,
        {
            "type": "session_info",
            "id": "nm",
            "parentId": last["id"],
            "name": "Refactor auth module",
        },
    )
    assert session_readers(home)["pi"].list_sessions()[0].title == "Refactor auth module"


# ---- SQLite harnesses ---------------------------------------------------------------------------


def test_hermes_rows_the_harness_marked_inactive_or_hidden_are_not_imported(home):
    c = case("hermes")
    c.install(home)
    con = sqlite3.connect(home / c.store_rel)
    con.execute("UPDATE messages SET active = 0 WHERE role = 'tool'")
    con.commit()
    t = session_readers(home)["hermes"].load_session(c.session_id)
    assert t.lossy.get("tool_results", 0) == 0
    con.execute("UPDATE sessions SET hidden = 1")
    con.commit()
    con.close()
    assert session_readers(home)["hermes"].list_sessions() == []


def test_goose_messages_the_harness_marks_not_user_visible_are_dropped_and_counted(home):
    c = case("goose")
    c.install(home)
    t = session_readers(home)["goose"].load_session(c.session_id)
    assert t.lossy["harness_context_dropped"] == 1
    assert all("turn-context" not in m.content for m in t.messages)


def test_opencode_subagent_child_sessions_are_not_listed_as_conversations(home):
    c = case("opencode")
    c.install(home)
    con = sqlite3.connect(home / c.store_rel)
    con.execute(
        "INSERT INTO session SELECT 'ses_child', project_id, workspace_id, ? , slug, directory, path, title, "
        "version, share_url, summary_additions, summary_deletions, summary_files, summary_diffs, metadata, cost, "
        "tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write, revert, permission, "
        "agent, model, time_created, time_updated, time_compacting, time_archived FROM session",
        (c.session_id,),
    )
    con.commit()
    con.close()
    ids = [h.source_session_id for h in session_readers(home)["opencode"].list_sessions()]
    assert ids == [c.session_id]


def test_cursor_subagent_logs_are_not_conversations(home):
    c = case("cursor")
    c.install(home)
    primary = c.primary(home)
    sub = primary.parent / "subagents" / "sub-1.jsonl"
    sub.parent.mkdir()
    sub.write_text(primary.read_text())
    assert [h.source_session_id for h in session_readers(home)["cursor"].list_sessions()] == [
        c.session_id
    ]


def test_cursor_user_text_is_the_query_not_the_harness_wrapper(home):
    c = case("cursor")
    c.install(home)
    t = session_readers(home)["cursor"].load_session(c.session_id)
    assert (
        t.messages[0].content.startswith("Run echo") and "<timestamp>" not in t.messages[0].content
    )
    assert t.lossy["harness_context_dropped"] == 1  # the injected <dynamic_tools> block


# ---- what /renew says about harnesses without a reader ---------------------------------------------


@pytest.mark.parametrize("name", ["aider", "letta", "roo", "grok"])
def test_a_harness_without_a_reader_says_exactly_what_is_missing_and_what_exists(name):
    out = renew_sources.no_reader_message(name)
    st = HARNESS_STATUS[name]
    assert not st.reader and "no session reader" in out
    assert "What is missing:" in out and st.missing.split(";")[0][:30] in out
    assert "What exists for it:" in out and "Format checked:" in out
    assert "claude-code" in out and "gemini" in out  # what works now includes the new readers


def test_grok_is_recorded_as_unverified_with_what_was_found():
    out = renew_sources.no_reader_message("grok")
    assert "unverified" in out and "Grok Bot" in out and "never been opened" in out


def test_every_shipped_reader_has_a_verified_status_and_no_unverified_one_ships():
    readers = set(session_readers())
    shipped = {n for n, s in HARNESS_STATUS.items() if s.reader}
    assert readers == shipped
    assert all(
        HARNESS_STATUS[n].verified_by in ("run", "real files on this machine") for n in shipped
    )
    assert all(
        s.verified_by in ("unverified", "docs", "run")
        for n, s in HARNESS_STATUS.items()
        if not s.reader
    )


def test_an_empty_source_explains_itself_including_the_part_of_its_history_that_is_not_read(
    home, tmp_path, monkeypatch
):
    monkeypatch.setattr("axiom.infra.orchestrator.session._default_principal", lambda: ME)
    agent = Agent(Session(principal_id=ME))
    out = renew.renew(agent, new_store(tmp_path), "", source="cursor")
    assert (
        "No cursor sessions were found" in out
        and "does not exist" in out
        and "Not read: Cursor" in out
    )
