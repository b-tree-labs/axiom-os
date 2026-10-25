# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""RIVET speaks while the budget is still there, and does not repeat itself."""

from __future__ import annotations

from axiom.extensions.builtins.release.actions_spend import (
    SpendReading,
    assess,
    load_last,
    save_last,
    should_speak,
    watched_repos,
)


def test_a_spent_budget_is_said_before_another_pull_request():
    text = assess(SpendReading(minutes_used=3000, included_minutes=3000, in_progress=0, open_prs=2))
    assert text is not None
    assert "spent" in text
    assert "Do not open" in text


def test_in_progress_runs_that_exceed_what_remains_stop_updates():
    text = assess(SpendReading(minutes_used=2900, included_minutes=3000, in_progress=6, open_prs=1))
    assert text is not None
    assert "Stop updating" in text


def test_many_open_pull_requests_are_folded_instead_of_opened():
    text = assess(SpendReading(minutes_used=2900, included_minutes=3000, in_progress=0, open_prs=8))
    assert text is not None
    assert "Fold" in text


def test_a_quiet_budget_says_nothing():
    assert assess(SpendReading(minutes_used=100, included_minutes=3000, in_progress=1, open_prs=2)) is None


def test_watched_repos_come_from_the_operator_list(tmp_path, monkeypatch):
    monkeypatch.delenv("AXIOM_ACTIONS_REPOS", raising=False)
    path = tmp_path / "agents" / "rivet" / "actions-repos"
    path.parent.mkdir(parents=True)
    path.write_text("# comment\norg/one\norg/two\n", encoding="utf-8")
    assert watched_repos(tmp_path) == ("org/one", "org/two")
    monkeypatch.setenv("AXIOM_ACTIONS_REPOS", "org/from-env")
    assert watched_repos(tmp_path) == ("org/from-env",)


def test_the_same_sentence_is_not_repeated(tmp_path):
    message = "Actions budget is spent (3 of 3 minutes). Do not open or update a pull request."
    assert should_speak(message, None)
    save_last(tmp_path, message)
    assert load_last(tmp_path) == message
    assert not should_speak(message, load_last(tmp_path))
    assert should_speak("different", load_last(tmp_path))
