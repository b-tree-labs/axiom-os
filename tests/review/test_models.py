# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.review.models — ReviewItem, ReviewSession, ReviewSessionStore."""

import pytest

from axiom.review.models import (
    ReviewDecision,
    ReviewItem,
    ReviewSession,
    ReviewSessionStore,
    _source_hash,
)

# ── fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def decision():
    return ReviewDecision(
        reviewer="alice",
        status="accepted",
        channel="cli",
        decided_at="2026-03-04T10:00:00Z",
    )


@pytest.fixture
def item():
    return ReviewItem(
        item_id="abc123",
        heading="## Project Update",
        content="Progress on the project this week.",
    )


@pytest.fixture
def session(item):
    return ReviewSession(
        session_id="draft_weekly_2026-03-04",
        session_type="draft",
        source="/path/to/weekly_summary.md",
        source_hash="abcdef1234567890",
        started_at="2026-03-04T09:00:00Z",
        items=[item],
    )


@pytest.fixture
def store(tmp_path):
    state_path = tmp_path / "review_state.json"
    return ReviewSessionStore(state_path=state_path)


# ── ReviewDecision ───────────────────────────────────────────────────

class TestReviewDecision:
    def test_roundtrip(self, decision):
        d = ReviewDecision.from_dict(decision.to_dict())
        assert d.reviewer == "alice"
        assert d.status == "accepted"
        assert d.channel == "cli"
        assert d.decided_at == "2026-03-04T10:00:00Z"

    def test_defaults(self):
        d = ReviewDecision(reviewer="bob", status="rejected")
        assert d.channel == "cli"
        assert d.edited_content == ""
        assert d.comment == ""

    def test_edited_content_preserved(self):
        d = ReviewDecision(
            reviewer="bob",
            status="edited",
            edited_content="fixed version",
        )
        roundtripped = ReviewDecision.from_dict(d.to_dict())
        assert roundtripped.edited_content == "fixed version"


# ── ReviewItem ───────────────────────────────────────────────────────

class TestReviewItem:
    def test_roundtrip(self, item):
        d = ReviewItem.from_dict(item.to_dict())
        assert d.item_id == "abc123"
        assert d.heading == "## Project Update"
        assert d.content == "Progress on the project this week."
        assert d.status == "pending"

    def test_decisions_roundtrip(self, item, decision):
        item.decisions.append(decision)
        d = ReviewItem.from_dict(item.to_dict())
        assert len(d.decisions) == 1
        assert d.decisions[0].reviewer == "alice"

    def test_resolve_status_any(self, item):
        """'any' mode: last decision wins."""
        item.decisions.append(ReviewDecision(reviewer="a", status="accepted"))
        item.decisions.append(ReviewDecision(reviewer="b", status="rejected"))
        assert item.resolve_status("any") == "rejected"

    def test_resolve_status_all_unanimous(self, item):
        """'all' mode: all agree -> that status."""
        item.required_reviewers = ["a", "b"]
        item.decisions.append(ReviewDecision(reviewer="a", status="accepted"))
        item.decisions.append(ReviewDecision(reviewer="b", status="accepted"))
        assert item.resolve_status("all") == "accepted"

    def test_resolve_status_all_missing_reviewer(self, item):
        """'all' mode: missing reviewer -> pending."""
        item.required_reviewers = ["a", "b"]
        item.decisions.append(ReviewDecision(reviewer="a", status="accepted"))
        assert item.resolve_status("all") == "pending"

    def test_resolve_status_all_disagreement(self, item):
        """'all' mode: disagreement -> pending."""
        item.required_reviewers = ["a", "b"]
        item.decisions.append(ReviewDecision(reviewer="a", status="accepted"))
        item.decisions.append(ReviewDecision(reviewer="b", status="rejected"))
        assert item.resolve_status("all") == "pending"

    def test_resolve_status_majority(self, item):
        """'majority' mode: >50% agree."""
        item.decisions.append(ReviewDecision(reviewer="a", status="accepted"))
        item.decisions.append(ReviewDecision(reviewer="b", status="accepted"))
        item.decisions.append(ReviewDecision(reviewer="c", status="rejected"))
        assert item.resolve_status("majority") == "accepted"

    def test_resolve_status_majority_tie(self, item):
        """'majority' mode: 50-50 -> pending."""
        item.decisions.append(ReviewDecision(reviewer="a", status="accepted"))
        item.decisions.append(ReviewDecision(reviewer="b", status="rejected"))
        assert item.resolve_status("majority") == "pending"

    def test_resolve_status_empty(self, item):
        """No decisions -> pending."""
        assert item.resolve_status("any") == "pending"


# ── ReviewSession ────────────────────────────────────────────────────

class TestReviewSession:
    def test_roundtrip(self, session):
        d = ReviewSession.from_dict(session.to_dict())
        assert d.session_id == "draft_weekly_2026-03-04"
        assert d.session_type == "draft"
        assert len(d.items) == 1
        assert d.consensus_mode == "any"

    def test_progress(self, session):
        reviewed, total = session.progress
        assert reviewed == 0
        assert total == 1

    def test_pending_items(self, session):
        assert len(session.pending_items) == 1
        session.items[0].status = "accepted"
        assert len(session.pending_items) == 0

    def test_reviewed_items(self, session):
        assert len(session.reviewed_items) == 0
        session.items[0].status = "accepted"
        assert len(session.reviewed_items) == 1

    def test_consensus_mode_default(self, session):
        assert session.consensus_mode == "any"


# ── ReviewSessionStore ───────────────────────────────────────────────

class TestReviewSessionStore:
    def test_save_and_get(self, store, session):
        store.save(session)
        loaded = store.get(session.session_id)
        assert loaded is not None
        assert loaded.session_id == session.session_id
        assert len(loaded.items) == 1

    def test_persistence_across_instances(self, tmp_path, session):
        state_path = tmp_path / "review_state.json"
        store1 = ReviewSessionStore(state_path=state_path)
        store1.save(session)

        store2 = ReviewSessionStore(state_path=state_path)
        loaded = store2.get(session.session_id)
        assert loaded is not None
        assert loaded.source_hash == session.source_hash

    def test_remove(self, store, session):
        store.save(session)
        assert store.remove(session.session_id)
        assert store.get(session.session_id) is None

    def test_remove_nonexistent(self, store):
        assert not store.remove("nonexistent")

    def test_list_active(self, store, session):
        store.save(session)
        assert len(store.list_active()) == 1

        # Mark all items reviewed
        session.items[0].status = "accepted"
        store.save(session)
        assert len(store.list_active()) == 0

    def test_find_by_source(self, store, session):
        store.save(session)
        found = store.find_by_source(session.source)
        assert found is not None
        assert found.session_id == session.session_id

    def test_find_by_source_none(self, store):
        assert store.find_by_source("/nonexistent") is None

    def test_corrupted_file(self, tmp_path):
        """Corrupted JSON file should not crash, just start empty."""
        state_path = tmp_path / "review_state.json"
        state_path.write_text("not valid json", encoding="utf-8")
        store = ReviewSessionStore(state_path=state_path)
        assert len(store.list_all()) == 0


# ── source_hash ──────────────────────────────────────────────────────

class TestSourceHash:
    def test_deterministic(self):
        assert _source_hash("hello") == _source_hash("hello")

    def test_different_for_different_content(self):
        assert _source_hash("hello") != _source_hash("world")

    def test_length(self):
        assert len(_source_hash("test")) == 16
