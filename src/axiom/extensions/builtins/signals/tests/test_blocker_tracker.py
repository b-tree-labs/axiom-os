# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the BlockerTracker."""


import pytest

from axiom.extensions.builtins.signals.blocker_tracker import BlockerTracker, _blocker_id
from axiom.extensions.builtins.signals.models import Signal


@pytest.fixture
def tracker(tmp_path):
    """Create a BlockerTracker with a temp state file."""
    state_path = tmp_path / "state" / "blocker_state.json"
    return BlockerTracker(state_path=state_path)


@pytest.fixture
def blocker_signals():
    """Sample blocker signals for testing."""
    return [
        Signal(
            source="freetext",
            timestamp="2026-03-01T10:00:00Z",
            raw_text="Blocked on API key",
            people=["Alice"],
            initiatives=["Project Alpha", "Project Beta"],
            signal_type="blocker",
            detail="Blocked on MS 365 API key for tracker publishing",
            confidence=0.8,
        ),
        Signal(
            source="freetext",
            timestamp="2026-03-01T11:00:00Z",
            raw_text="Data access issue",
            people=["Bob"],
            initiatives=["Project Gamma"],
            signal_type="blocker",
            detail="Bob needs data access permissions for gamma dataset",
            confidence=0.7,
        ),
    ]


class TestBlockerTracker:

    def test_new_blocker_tracked(self, tracker, blocker_signals):
        """New blocker signals create TrackedBlocker entries."""
        tracker.update(blocker_signals)
        active = tracker.get_active_blockers()
        assert len(active) == 2

    def test_recurring_blocker_increments_count(self, tracker, blocker_signals):
        """Reporting the same blocker twice increments times_reported."""
        tracker.update(blocker_signals)
        tracker.update(blocker_signals)

        active = tracker.get_active_blockers()
        for blocker in active:
            assert blocker.times_reported == 2

    def test_cross_cutting_detection(self, tracker, blocker_signals):
        """Blockers affecting 2+ initiatives are marked cross-cutting."""
        tracker.update(blocker_signals)
        cross = tracker.get_cross_cutting_blockers()
        assert len(cross) == 1
        assert "Project Alpha" in cross[0].initiatives
        assert "Project Beta" in cross[0].initiatives
        assert cross[0].is_cross_cutting is True

    def test_resolved_blocker_excluded(self, tracker, blocker_signals):
        """Resolved blockers don't appear in active list."""
        tracker.update(blocker_signals)
        active = tracker.get_active_blockers()
        assert len(active) == 2

        # Resolve one
        bid = active[0].blocker_id
        tracker.resolve_blocker(bid)
        active = tracker.get_active_blockers()
        assert len(active) == 1

    def test_persistence(self, tmp_path, blocker_signals):
        """Blocker state survives save/reload cycle."""
        state_path = tmp_path / "state" / "blocker_state.json"
        tracker1 = BlockerTracker(state_path=state_path)
        tracker1.update(blocker_signals)
        assert len(tracker1.get_active_blockers()) == 2

        # Reload from same path
        tracker2 = BlockerTracker(state_path=state_path)
        assert len(tracker2.get_active_blockers()) == 2

    def test_ignores_non_blocker_signals(self, tracker):
        """Non-blocker signals are ignored."""
        signals = [
            Signal(
                source="freetext",
                timestamp="2026-03-01T10:00:00Z",
                raw_text="Progress note",
                initiatives=["Project Alpha"],
                signal_type="progress",
                detail="Made progress on feature X",
                confidence=0.8,
            ),
        ]
        tracker.update(signals)
        assert len(tracker.get_active_blockers()) == 0

    def test_cross_cutting_proposed_action(self, tracker, blocker_signals):
        """Cross-cutting blockers get 'create_issue' proposed action."""
        tracker.update(blocker_signals)
        cross = tracker.get_cross_cutting_blockers()
        assert cross[0].proposed_action == "create_issue"

    def test_person_specific_proposed_action(self, tracker, blocker_signals):
        """Person-specific blockers get 'send_followup' proposed action."""
        tracker.update(blocker_signals)
        active = tracker.get_active_blockers()
        non_cross = [b for b in active if not b.is_cross_cutting]
        assert len(non_cross) == 1
        assert non_cross[0].proposed_action == "send_followup"

    def test_blocker_id_stability(self):
        """Same detail text always produces the same blocker ID."""
        detail = "Blocked on MS 365 API key"
        id1 = _blocker_id(detail)
        id2 = _blocker_id(detail)
        assert id1 == id2
        # Case insensitive
        id3 = _blocker_id(detail.upper())
        assert id1 == id3
