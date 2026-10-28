# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A session we cannot read is not a session that was never there.

`list_sessions` says "Return all sessions" and then skips, with a bare
`continue`, every file it cannot parse. A truncated session file therefore
disappears from `axi session list` entirely — no row, no warning, no trace —
and `find_by_name` falls back to the same scan and reports the session unknown.

The user sees a complete-looking list that is quietly missing their session.

This is the milder relative of the state-file bug: nothing is destroyed here,
because the callers that follow a failed load either raise or return rather than
writing over the file. What is lost is the knowledge that anything was skipped.
So the fix is proportionate — the listing keeps working and keeps its shape, but
it can no longer claim completeness it does not have.
"""

from __future__ import annotations

import json
import logging

import pytest

from axiom.memory import session as sess


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    d = tmp_path / "sessions"
    d.mkdir()
    monkeypatch.setattr(sess, "_sessions_dir", lambda: d)
    return d


def _good(d, sid: str, name: str):
    (d / f"{sid}.json").write_text(json.dumps({
        "session_id": f"{sess.SESSION_URI_PREFIX}{sid}",
        "name": name,
        "principal_id": "@ben:ut",
        "created_at": "2026-09-14T00:00:00+00:00",
        "last_active_at": "2026-09-14T00:00:00+00:00",
    }))


def test_a_readable_session_is_listed(sessions_dir):
    _good(sessions_dir, "aaaa", "one")
    assert [m.name for m in sess.list_sessions()] == ["one"]


def test_an_unreadable_session_does_not_vanish_silently(sessions_dir, caplog):
    _good(sessions_dir, "aaaa", "one")
    (sessions_dir / "bbbb.json").write_text('{"session_id": "axiom://sess/bbbb", "na')

    with caplog.at_level(logging.WARNING, logger="axiom.memory.session"):
        listed = sess.list_sessions()

    assert [m.name for m in listed] == ["one"], "readable sessions must still list"
    assert any("bbbb" in r.message or "bbbb" in str(r.args) for r in caplog.records), (
        "an unreadable session file was skipped without a word: "
        f"{[r.message for r in caplog.records]}"
    )


def test_the_listing_still_works_when_every_file_is_unreadable(sessions_dir, caplog):
    (sessions_dir / "cccc.json").write_text("{ truncated")
    with caplog.at_level(logging.WARNING, logger="axiom.memory.session"):
        assert sess.list_sessions() == []
    assert caplog.records, "an empty listing gave no hint that a file was skipped"
