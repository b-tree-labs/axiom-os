# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""/copy puts a reply on the clipboard, with its origin stamp, and never a secret (ADR-168, C6)."""

from __future__ import annotations

from axiom.extensions.builtins.chat.tests._transcript_support import (
    FAKE_GH_TOKEN,
    FAKE_OPENAI_KEY,
    fake_clipboard,
    make_agent,
    no_clipboard,
    run,
)

TURNS = [("q1", "first answer"), ("q2", "second answer"), ("q3", "third answer")]


def test_copies_the_last_reply_with_its_origin_stamp(tmp_path, monkeypatch):
    clip = fake_clipboard(tmp_path, monkeypatch)
    agent = make_agent(monkeypatch, TURNS)
    agent.gateway.last_selected = agent.gateway.providers[0]  # a turn has been answered
    out = run(agent, "/copy")
    text = clip.read_text()
    assert text.startswith("third answer")
    assert text.rstrip().splitlines()[-1].startswith("↳ ")  # the stamp, as the last line
    assert "documents: this machine" in text  # sources in words, not "RAG: LOCAL"
    assert "Copied" in out


def test_bare_copies_the_reply_alone(tmp_path, monkeypatch):
    clip = fake_clipboard(tmp_path, monkeypatch)
    run(make_agent(monkeypatch, TURNS), "/copy --bare")
    assert clip.read_text() == "third answer"


def test_n_selects_the_nth_last_reply(tmp_path, monkeypatch):
    clip = fake_clipboard(tmp_path, monkeypatch)
    run(make_agent(monkeypatch, TURNS), "/copy 2 --bare")
    assert clip.read_text() == "second answer"


def test_an_n_beyond_the_transcript_says_how_many_replies_exist(tmp_path, monkeypatch):
    clip = fake_clipboard(tmp_path, monkeypatch)
    out = run(make_agent(monkeypatch, TURNS), "/copy 9")
    assert "3" in out and not clip.exists()


def test_a_secret_is_refused_by_class_and_never_echoed_or_copied(tmp_path, monkeypatch):
    clip = fake_clipboard(tmp_path, monkeypatch)
    agent = make_agent(monkeypatch, [("q", f"use key {FAKE_OPENAI_KEY} to log in")])
    out = run(agent, "/copy")
    assert "openai_key" in out
    assert FAKE_OPENAI_KEY not in out
    assert not clip.exists()


def test_a_secret_in_the_stamp_free_bare_form_is_refused_too(tmp_path, monkeypatch):
    clip = fake_clipboard(tmp_path, monkeypatch)
    agent = make_agent(monkeypatch, [("q", f"token {FAKE_GH_TOKEN}")])
    out = run(agent, "/copy --bare")
    assert "github_token" in out and not clip.exists()


def test_without_a_clipboard_tool_it_says_so_and_offers_export(tmp_path, monkeypatch, capsys):
    no_clipboard(tmp_path, monkeypatch)
    out = run(make_agent(monkeypatch, TURNS), "/copy")
    assert "no clipboard tool" in out.lower()
    assert "/export" in out
    assert "Copied" not in out


def test_an_empty_transcript_says_there_is_nothing_to_copy(tmp_path, monkeypatch):
    fake_clipboard(tmp_path, monkeypatch)
    out = run(make_agent(monkeypatch), "/copy")
    assert "no assistant reply" in out.lower()


def test_it_is_not_offered_headless(tmp_path, monkeypatch):
    fake_clipboard(tmp_path, monkeypatch)
    out = run(make_agent(monkeypatch, TURNS), "/copy", surface="headless")
    assert "not available" in out


def test_an_older_reply_or_an_unanswered_session_gets_no_stamp_and_says_why(tmp_path, monkeypatch):
    clip = fake_clipboard(tmp_path, monkeypatch)
    agent = make_agent(monkeypatch, TURNS)
    agent.gateway.last_selected = agent.gateway.providers[0]
    out = run(agent, "/copy 2")
    assert clip.read_text() == "second answer" and "No origin stamp" in out
    out = run(make_agent(monkeypatch, TURNS), "/copy")  # nothing answered yet: origin is only a prediction
    assert clip.read_text() == "third answer" and "No origin stamp" in out
