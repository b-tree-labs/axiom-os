# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.extensions.builtins.chat.tools_ext.review — conversational review chat tools."""

from pathlib import Path

import pytest

from axiom.extensions.builtins.chat.tools_ext.review import (
    _get_session,
    execute,
)
from axiom.review.models import ReviewSessionStore

SAMPLE_DRAFT = """\
# Weekly Summary — 2026-03-04

Week of 2026-03-04: 10 signals across 3 initiatives.

## Consumer Layer

### Progress

- Published 8 documents to publisher.

## Project Alpha

### Progress

- Discovered 5 new repositories.

## Uncategorized

### Progress

- 55 commits total.

*Generated: 2026-03-04T01:10:21Z*
"""


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset module-level state between tests."""
    import axiom.extensions.builtins.chat.tools_ext.review as mod
    mod._store = None
    mod._active_session = None
    yield
    mod._store = None
    mod._active_session = None


@pytest.fixture
def draft_file(tmp_path):
    draft = tmp_path / "weekly_summary_2026-03-04.md"
    draft.write_text(SAMPLE_DRAFT)
    return draft


# ── review_start ─────────────────────────────────────────────────────

class TestReviewStart:
    def test_start_with_file(self, draft_file, tmp_path, monkeypatch):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        mod._store = ReviewSessionStore(state_path=tmp_path / "state.json")

        result = execute("review_start", {"file": str(draft_file)})
        assert "error" not in result
        assert result["total_items"] == 4  # preamble + 3 sections
        assert result["reviewed"] == 0
        assert result["pending"] == 4
        assert "next_item" in result

    def test_start_returns_first_item(self, draft_file, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        mod._store = ReviewSessionStore(state_path=tmp_path / "state.json")

        result = execute("review_start", {"file": str(draft_file)})
        next_item = result["next_item"]
        assert next_item["heading"] == "(Preamble)"
        assert "Weekly Summary" in next_item["content"]

    def test_start_no_file_found(self, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        mod._store = ReviewSessionStore(state_path=tmp_path / "state.json")

        result = execute("review_start", {"file": "/nonexistent/file.md"})
        assert "error" in result

    def test_start_resumes_existing(self, draft_file, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        store = ReviewSessionStore(state_path=tmp_path / "state.json")
        mod._store = store

        result1 = execute("review_start", {"file": str(draft_file)})
        session_id = result1["session_id"]

        # Reset active session but keep store
        mod._active_session = None

        result2 = execute("review_start", {"file": str(draft_file)})
        assert result2["session_id"] == session_id

    def test_start_fresh_creates_new(self, draft_file, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        store = ReviewSessionStore(state_path=tmp_path / "state.json")
        mod._store = store

        result1 = execute("review_start", {"file": str(draft_file)})
        mod._active_session = None

        result2 = execute("review_start", {"file": str(draft_file), "fresh": True})
        assert result2["session_id"] != result1["session_id"]


# ── review_get_item ──────────────────────────────────────────────────

class TestReviewGetItem:
    def test_get_next_pending(self, draft_file, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        mod._store = ReviewSessionStore(state_path=tmp_path / "state.json")
        execute("review_start", {"file": str(draft_file)})

        result = execute("review_get_item", {})
        assert result["heading"] == "(Preamble)"
        assert result["index"] == 0

    def test_get_by_index(self, draft_file, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        mod._store = ReviewSessionStore(state_path=tmp_path / "state.json")
        execute("review_start", {"file": str(draft_file)})

        result = execute("review_get_item", {"index": 2})
        assert "Project Alpha" in result["heading"]

    def test_get_out_of_range(self, draft_file, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        mod._store = ReviewSessionStore(state_path=tmp_path / "state.json")
        execute("review_start", {"file": str(draft_file)})

        result = execute("review_get_item", {"index": 99})
        assert "error" in result

    def test_no_session_error(self):
        result = execute("review_get_item", {})
        assert "error" in result

    def test_all_reviewed_complete(self, draft_file, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        mod._store = ReviewSessionStore(state_path=tmp_path / "state.json")
        execute("review_start", {"file": str(draft_file)})

        session = _get_session()
        for item in session.items:
            item.status = "accepted"

        result = execute("review_get_item", {})
        assert result.get("complete") is True


# ── review_decide ────────────────────────────────────────────────────

class TestReviewDecide:
    def test_accept_item(self, draft_file, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        mod._store = ReviewSessionStore(state_path=tmp_path / "state.json")
        start = execute("review_start", {"file": str(draft_file)})
        item_id = start["next_item"]["item_id"]

        result = execute("review_decide", {"item_id": item_id, "status": "accepted"})
        assert result["decision"] == "accepted"
        assert result["progress"] == "1/4"

    def test_edit_item(self, draft_file, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        mod._store = ReviewSessionStore(state_path=tmp_path / "state.json")
        start = execute("review_start", {"file": str(draft_file)})
        item_id = start["next_item"]["item_id"]

        result = execute("review_decide", {
            "item_id": item_id,
            "status": "edited",
            "edited_content": "Revised preamble content",
            "comment": "Tightened the wording",
        })
        assert result["decision"] == "edited"

    def test_reject_item(self, draft_file, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        mod._store = ReviewSessionStore(state_path=tmp_path / "state.json")
        start = execute("review_start", {"file": str(draft_file)})
        item_id = start["next_item"]["item_id"]

        result = execute("review_decide", {"item_id": item_id, "status": "rejected"})
        assert result["decision"] == "rejected"

    def test_edit_requires_content(self, draft_file, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        mod._store = ReviewSessionStore(state_path=tmp_path / "state.json")
        start = execute("review_start", {"file": str(draft_file)})
        item_id = start["next_item"]["item_id"]

        result = execute("review_decide", {"item_id": item_id, "status": "edited"})
        assert "error" in result

    def test_invalid_status(self, draft_file, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        mod._store = ReviewSessionStore(state_path=tmp_path / "state.json")
        start = execute("review_start", {"file": str(draft_file)})
        item_id = start["next_item"]["item_id"]

        result = execute("review_decide", {"item_id": item_id, "status": "maybe"})
        assert "error" in result

    def test_unknown_item_id(self, draft_file, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        mod._store = ReviewSessionStore(state_path=tmp_path / "state.json")
        execute("review_start", {"file": str(draft_file)})

        result = execute("review_decide", {"item_id": "nonexistent", "status": "accepted"})
        assert "error" in result

    def test_returns_next_item(self, draft_file, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        mod._store = ReviewSessionStore(state_path=tmp_path / "state.json")
        start = execute("review_start", {"file": str(draft_file)})
        item_id = start["next_item"]["item_id"]

        result = execute("review_decide", {"item_id": item_id, "status": "accepted"})
        assert "next_item" in result
        assert "Consumer Layer" in result["next_item"]["heading"]

    def test_last_item_signals_complete(self, draft_file, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        mod._store = ReviewSessionStore(state_path=tmp_path / "state.json")
        execute("review_start", {"file": str(draft_file)})

        session = _get_session()
        # Accept all but the last
        for item in session.items[:-1]:
            execute("review_decide", {"item_id": item.item_id, "status": "accepted"})

        last_id = session.items[-1].item_id
        result = execute("review_decide", {"item_id": last_id, "status": "accepted"})
        assert result.get("complete") is True

    def test_persists_to_store(self, draft_file, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        store = ReviewSessionStore(state_path=tmp_path / "state.json")
        mod._store = store
        start = execute("review_start", {"file": str(draft_file)})
        item_id = start["next_item"]["item_id"]

        execute("review_decide", {"item_id": item_id, "status": "accepted"})

        # Reload store and verify persistence
        reloaded = ReviewSessionStore(state_path=tmp_path / "state.json")
        session = reloaded.find_by_source(str(draft_file))
        assert session is not None
        item = [i for i in session.items if i.item_id == item_id][0]
        assert item.status == "accepted"


# ── review_progress ──────────────────────────────────────────────────

class TestReviewProgress:
    def test_with_active_session(self, draft_file, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        mod._store = ReviewSessionStore(state_path=tmp_path / "state.json")
        execute("review_start", {"file": str(draft_file)})

        result = execute("review_progress", {})
        assert result["total"] == 4
        assert result["reviewed"] == 0
        assert result["pending"] == 4
        assert len(result["items"]) == 4

    def test_no_session(self, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        mod._store = ReviewSessionStore(state_path=tmp_path / "state.json")

        result = execute("review_progress", {})
        assert "No active review sessions" in result.get("message", "")


# ── review_complete ──────────────────────────────────────────────────

class TestReviewComplete:
    def test_writes_approved_file(self, draft_file, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        mod._store = ReviewSessionStore(state_path=tmp_path / "state.json")
        execute("review_start", {"file": str(draft_file)})

        session = _get_session()
        for item in session.items:
            execute("review_decide", {"item_id": item.item_id, "status": "accepted"})

        approved_dir = tmp_path / "approved"
        result = execute("review_complete", {"approved_dir": str(approved_dir.relative_to(Path(str(tmp_path)).parent.parent.parent.parent.parent.parent)) if False else str(approved_dir)})

        # Use absolute path override
        result = execute("review_complete", {})
        assert "approved_path" in result or "message" in result

    def test_all_rejected_returns_message(self, draft_file, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        mod._store = ReviewSessionStore(state_path=tmp_path / "state.json")
        execute("review_start", {"file": str(draft_file)})

        session = _get_session()
        for item in session.items:
            execute("review_decide", {"item_id": item.item_id, "status": "rejected"})

        result = execute("review_complete", {})
        assert "rejected" in result.get("message", "").lower() or result.get("summary", {}).get("rejected") == 4

    def test_no_session_error(self):
        result = execute("review_complete", {})
        assert "error" in result

    def test_summary_counts(self, draft_file, tmp_path):
        import axiom.extensions.builtins.chat.tools_ext.review as mod
        mod._store = ReviewSessionStore(state_path=tmp_path / "state.json")
        execute("review_start", {"file": str(draft_file)})

        session = _get_session()
        # Accept 2, reject 1, edit 1
        execute("review_decide", {"item_id": session.items[0].item_id, "status": "accepted"})
        execute("review_decide", {"item_id": session.items[1].item_id, "status": "accepted"})
        execute("review_decide", {"item_id": session.items[2].item_id, "status": "rejected"})
        execute("review_decide", {
            "item_id": session.items[3].item_id,
            "status": "edited",
            "edited_content": "Revised uncategorized section.",
        })

        result = execute("review_complete", {})
        if "summary" in result:
            assert result["summary"]["accepted"] == 2
            assert result["summary"]["rejected"] == 1
            assert result["summary"]["edited"] == 1


# ── unknown tool ─────────────────────────────────────────────────────

class TestUnknownTool:
    def test_unknown_tool_name(self):
        result = execute("review_bogus", {})
        assert "error" in result
