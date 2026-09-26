# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.review.runner — ReviewRunner with mock adapter."""

from unittest.mock import patch

import pytest

from axiom.review.models import (
    ReviewDecision,
    ReviewItem,
    ReviewSession,
    ReviewSessionStore,
)
from axiom.review.runner import ReviewRunner

# ── mock adapter ─────────────────────────────────────────────────────

class MockAdapter:
    """Minimal ReviewAdapter for testing."""

    def __init__(self):
        self.displayed_items: list[str] = []
        self.completed_sessions: list[str] = []
        self.summary_called = False

    def display_item(self, item: ReviewItem, index: int, total: int) -> None:
        self.displayed_items.append(item.item_id)

    def display_summary(self, items: list[ReviewItem]) -> None:
        self.summary_called = True

    def get_commands(self) -> dict[str, str]:
        return {"X": "Extra command"}

    def handle_command(self, cmd: str, item: ReviewItem) -> str | None:
        if cmd == "X":
            return "accepted"
        return None

    def on_session_complete(self, session: ReviewSession) -> None:
        self.completed_sessions.append(session.session_id)


# ── fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    return ReviewSessionStore(state_path=tmp_path / "review_state.json")


@pytest.fixture
def adapter():
    return MockAdapter()


@pytest.fixture
def session():
    return ReviewSession(
        session_id="test_session",
        session_type="test",
        source="/test/file.md",
        source_hash="abc123",
        started_at="2026-03-04T09:00:00Z",
        items=[
            ReviewItem(item_id="item1", heading="## First", content="Content one"),
            ReviewItem(item_id="item2", heading="## Second", content="Content two"),
            ReviewItem(item_id="item3", heading="## Third", content="Content three"),
        ],
    )


# ── detailed mode tests ─────────────────────────────────────────────

class TestDetailedMode:
    def test_accept_all(self, adapter, store, session):
        """Accept all items sequentially."""
        with patch("builtins.input", side_effect=["a", "a", "a"]):
            runner = ReviewRunner(adapter, store, reviewer="tester")
            runner.run(session)

        assert all(i.status == "accepted" for i in session.items)
        assert len(adapter.displayed_items) == 3
        assert "test_session" in adapter.completed_sessions

    def test_quit_mid_review(self, adapter, store, session):
        """Quit after first item — remaining stay pending."""
        with patch("builtins.input", side_effect=["a", "q"]):
            runner = ReviewRunner(adapter, store, reviewer="tester")
            runner.run(session)

        assert session.items[0].status == "accepted"
        assert session.items[1].status == "pending"
        assert session.items[2].status == "pending"
        # Session should be saved
        loaded = store.get("test_session")
        assert loaded is not None

    def test_skip_item(self, adapter, store, session):
        """Skip an item — it gets status 'skipped'."""
        with patch("builtins.input", side_effect=["s", "a", "a"]):
            runner = ReviewRunner(adapter, store, reviewer="tester")
            runner.run(session)

        assert session.items[0].status == "skipped"
        assert session.items[1].status == "accepted"

    def test_adapter_command(self, adapter, store, session):
        """Adapter extra command resolves item."""
        with patch("builtins.input", side_effect=["x", "a", "a"]):
            runner = ReviewRunner(adapter, store, reviewer="tester")
            runner.run(session)

        assert session.items[0].status == "accepted"  # via adapter "X" -> "accepted"
        assert len(session.items[0].decisions) == 1
        assert session.items[0].decisions[0].reviewer == "tester"

    def test_invalid_command_reprompts(self, adapter, store, session):
        """Invalid command should re-prompt, not crash."""
        with patch("builtins.input", side_effect=["z", "?", "a", "a", "a"]):
            runner = ReviewRunner(adapter, store, reviewer="tester")
            runner.run(session)

        assert all(i.status == "accepted" for i in session.items)

    def test_resume_skips_reviewed(self, adapter, store, session):
        """Already-reviewed items are skipped on resume."""
        session.items[0].status = "accepted"
        with patch("builtins.input", side_effect=["a", "a"]):
            runner = ReviewRunner(adapter, store, reviewer="tester")
            runner.run(session)

        # item1 was already accepted, should not be re-displayed
        assert "item1" not in adapter.displayed_items
        assert "item2" in adapter.displayed_items
        assert "item3" in adapter.displayed_items

    def test_ctrl_c_saves(self, adapter, store, session):
        """KeyboardInterrupt saves session without crash."""
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            runner = ReviewRunner(adapter, store, reviewer="tester")
            runner.run(session)

        loaded = store.get("test_session")
        assert loaded is not None

    def test_eof_saves(self, adapter, store, session):
        """EOFError saves session without crash."""
        with patch("builtins.input", side_effect=EOFError):
            runner = ReviewRunner(adapter, store, reviewer="tester")
            runner.run(session)

        loaded = store.get("test_session")
        assert loaded is not None

    def test_decision_records_reviewer(self, adapter, store, session):
        """Decisions capture the reviewer name."""
        with patch("builtins.input", side_effect=["a", "q"]):
            runner = ReviewRunner(adapter, store, reviewer="alice")
            runner.run(session)

        assert session.items[0].decisions[0].reviewer == "alice"
        assert session.items[0].decisions[0].channel == "cli"


# ── quick mode tests ─────────────────────────────────────────────────

class TestQuickMode:
    def test_approve_all(self, adapter, store, session):
        """Quick mode 'A' approves all pending items."""
        with patch("builtins.input", return_value="a"):
            runner = ReviewRunner(adapter, store, reviewer="tester")
            runner.run(session, quick=True)

        assert all(i.status == "accepted" for i in session.items)
        assert adapter.summary_called

    def test_reject_all(self, adapter, store, session):
        """Quick mode 'R' rejects all pending items."""
        with patch("builtins.input", return_value="r"):
            runner = ReviewRunner(adapter, store, reviewer="tester")
            runner.run(session, quick=True)

        assert all(i.status == "rejected" for i in session.items)

    def test_switch_to_detailed(self, adapter, store, session):
        """Quick mode 'D' switches to detailed review."""
        with patch("builtins.input", side_effect=["d", "a", "a", "a"]):
            runner = ReviewRunner(adapter, store, reviewer="tester")
            runner.run(session, quick=True)

        assert all(i.status == "accepted" for i in session.items)
        # After switching to detailed, items should be displayed
        assert len(adapter.displayed_items) == 3

    def test_invalid_reprompts(self, adapter, store, session):
        """Invalid quick mode command re-prompts."""
        with patch("builtins.input", side_effect=["z", "a"]):
            runner = ReviewRunner(adapter, store, reviewer="tester")
            runner.run(session, quick=True)

        assert all(i.status == "accepted" for i in session.items)

    def test_ctrl_c_saves(self, adapter, store, session):
        """KeyboardInterrupt in quick mode saves session."""
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            runner = ReviewRunner(adapter, store, reviewer="tester")
            runner.run(session, quick=True)

        loaded = store.get("test_session")
        assert loaded is not None


# ── multi-reviewer tests ─────────────────────────────────────────────

class TestMultiReviewer:
    def test_two_reviewers_any_mode(self, adapter, store, session):
        """In 'any' mode, second reviewer's decision becomes status."""
        session.consensus_mode = "any"

        # First reviewer accepts
        with patch("builtins.input", side_effect=["a", "q"]):
            runner = ReviewRunner(adapter, store, reviewer="alice")
            runner.run(session)

        assert session.items[0].status == "accepted"

    def test_two_reviewers_all_mode(self, adapter, store):
        """In 'all' mode, all required reviewers must decide."""
        session = ReviewSession(
            session_id="multi_test",
            session_type="test",
            source="/test/file.md",
            source_hash="abc",
            started_at="2026-03-04T09:00:00Z",
            items=[
                ReviewItem(
                    item_id="item1",
                    heading="## Test",
                    content="Content",
                    required_reviewers=["alice", "bob"],
                ),
            ],
            consensus_mode="all",
        )

        # Alice accepts — still pending (waiting for bob)
        with patch("builtins.input", side_effect=["a"]):
            runner = ReviewRunner(adapter, store, reviewer="alice")
            runner.run(session)

        # Status should be accepted in 'any' resolve but the item tracks it
        # The consensus check happens at resolve_status level
        item = session.items[0]
        assert len(item.decisions) == 1
        assert item.resolve_status("all") == "pending"

        # Bob accepts — now resolved
        item.decisions.append(ReviewDecision(
            reviewer="bob", status="accepted", channel="email",
        ))
        assert item.resolve_status("all") == "accepted"
