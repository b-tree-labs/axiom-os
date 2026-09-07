# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the signal synthesizer."""

import pytest

from axiom.extensions.builtins.signals.models import Signal
from axiom.extensions.builtins.signals.synthesizer import Synthesizer


@pytest.fixture
def sample_signals():
    """Create a set of test signals for synthesis."""
    return [
        Signal(
            source="gitlab_diff",
            timestamp="2026-02-15T10:00:00Z",
            raw_text="Added feature X",
            people=["Alice"],
            initiatives=["Project Alpha"],
            signal_type="progress",
            detail="Alice: 3 new commits in alpha-project",
            confidence=1.0,
        ),
        Signal(
            source="gitlab_diff",
            timestamp="2026-02-15T10:00:00Z",
            raw_text="Closed issue #5",
            people=["Bob"],
            initiatives=["Project Alpha"],
            signal_type="progress",
            detail="Issue closed: #5 Fix CI pipeline",
            confidence=1.0,
        ),
        Signal(
            source="freetext",
            timestamp="2026-02-14T10:00:00Z",
            raw_text="Blocked on data access",
            people=["Charlie"],
            initiatives=["Project Beta"],
            signal_type="blocker",
            detail="Charlie blocked on data access permissions",
            confidence=0.7,
        ),
        Signal(
            source="gitlab_diff",
            timestamp="2026-02-15T10:00:00Z",
            raw_text="No recent commits",
            initiatives=["Project Gamma"],
            signal_type="status_change",
            detail="Repository stale: gamma-project",
            confidence=1.0,
        ),
    ]


class TestSynthesizer:
    """Tests for changelog synthesis."""

    def test_synthesize_groups_by_initiative(self, sample_signals):
        synth = Synthesizer()
        changelog = synth.synthesize(sample_signals, date="2026-02-15")

        initiatives = {e.initiative for e in changelog.entries}
        assert "Project Alpha" in initiatives
        assert "Project Beta" in initiatives
        assert "Project Gamma" in initiatives

    def test_synthesize_preserves_signal_types(self, sample_signals):
        synth = Synthesizer()
        changelog = synth.synthesize(sample_signals)

        types = {e.signal_type for e in changelog.entries}
        assert "progress" in types
        assert "blocker" in types
        assert "status_change" in types

    def test_synthesize_builds_summary(self, sample_signals):
        synth = Synthesizer()
        changelog = synth.synthesize(sample_signals, date="2026-02-15")

        assert "4 signals" in changelog.summary
        assert "3 initiatives" in changelog.summary

    def test_synthesize_empty_signals(self):
        synth = Synthesizer()
        changelog = synth.synthesize([])
        assert len(changelog.entries) == 0
        assert "0 signals" in changelog.summary

    def test_write_changelog(self, sample_signals, tmp_path):
        synth = Synthesizer(drafts_dir=tmp_path)
        changelog = synth.synthesize(sample_signals, date="2026-02-15")
        path = synth.write_changelog(changelog)

        assert path.exists()
        assert path.name == "changelog_2026-02-15.md"

        content = path.read_text()
        assert "Project Alpha" in content
        assert "progress" in content

    def test_write_weekly_summary(self, sample_signals, tmp_path):
        synth = Synthesizer(drafts_dir=tmp_path)
        changelog = synth.synthesize(sample_signals, date="2026-02-15")
        path = synth.write_weekly_summary(changelog)

        assert path.exists()
        assert path.name == "weekly_summary_2026-02-15.md"

        content = path.read_text()
        assert "## Project Alpha" in content
        assert "### Blockers" in content or "### Progress" in content

    def test_changelog_table_format(self, sample_signals, tmp_path):
        """Verify the changelog uses proper markdown table format."""
        synth = Synthesizer(drafts_dir=tmp_path)
        changelog = synth.synthesize(sample_signals, date="2026-02-15")
        path = synth.write_changelog(changelog)

        content = path.read_text()
        lines = content.strip().splitlines()

        # Should have table header and separator
        table_lines = [ln for ln in lines if ln.startswith("|")]
        assert len(table_lines) >= 3  # Header + separator + at least 1 row

    def test_uncategorized_signals(self, tmp_path):
        """Signals without initiatives get grouped under 'Uncategorized'."""
        signals = [
            Signal(
                source="freetext",
                timestamp="2026-02-15T10:00:00Z",
                raw_text="Random note",
                signal_type="raw",
                detail="Something happened",
                confidence=0.3,
            ),
        ]
        synth = Synthesizer(drafts_dir=tmp_path)
        changelog = synth.synthesize(signals)

        assert any(e.initiative == "Uncategorized" for e in changelog.entries)


class TestToneFilter:
    """Tests for the constructive tone filter."""

    def test_blocking_rewrite(self):
        detail = "Alice is blocking the deployment"
        result = Synthesizer._apply_tone_filter(detail)
        assert "blocking" not in result.lower()
        assert "pending" in result.lower()

    def test_hasnt_responded_rewrite(self):
        detail = "John hasn't responded"
        result = Synthesizer._apply_tone_filter(detail)
        assert "hasn't responded" not in result
        assert "Awaiting response" in result

    def test_has_not_responded_rewrite(self):
        detail = "John has not responded"
        result = Synthesizer._apply_tone_filter(detail)
        assert "has not responded" not in result
        assert "Awaiting response" in result

    def test_except_for_person_rewrite(self):
        detail = "All data collection completed except for Clarno"
        result = Synthesizer._apply_tone_filter(detail)
        assert "except for" not in result.lower()
        assert "outstanding" in result.lower()

    def test_is_late_rewrite(self):
        detail = "Bob is late"
        result = Synthesizer._apply_tone_filter(detail)
        assert "is late" not in result
        assert "Timeline adjustment" in result

    def test_clean_text_unchanged(self):
        detail = "Completed milestone 3 ahead of schedule"
        result = Synthesizer._apply_tone_filter(detail)
        assert result == detail

    def test_failed_to_rewrite(self):
        detail = "Alice failed to submit the report"
        result = Synthesizer._apply_tone_filter(detail)
        assert "failed to" not in result
        assert "pending" in result.lower()


class TestStrategicOrdering:
    """Tests for strategic weight ordering in synthesis."""

    def test_higher_weight_first(self, tmp_path):
        """Initiatives with higher strategic weight appear first."""
        from axiom.extensions.builtins.signals.correlator import Correlator, Initiative

        # Create a correlator with known weights
        correlator = Correlator.__new__(Correlator)
        correlator.config_dir = tmp_path
        correlator.people = []
        correlator.initiatives = [
            Initiative(id="1", name="Low Priority", strategic_weight=1),
            Initiative(id="2", name="Critical Project", strategic_weight=5),
            Initiative(id="3", name="Medium Project", strategic_weight=3),
        ]

        signals = [
            Signal(
                source="freetext", timestamp="2026-03-01T10:00:00Z",
                raw_text="x", initiatives=["Low Priority"],
                signal_type="progress", detail="Progress on low", confidence=0.8,
            ),
            Signal(
                source="freetext", timestamp="2026-03-01T10:00:00Z",
                raw_text="x", initiatives=["Critical Project"],
                signal_type="progress", detail="Progress on critical", confidence=0.8,
            ),
            Signal(
                source="freetext", timestamp="2026-03-01T10:00:00Z",
                raw_text="x", initiatives=["Medium Project"],
                signal_type="progress", detail="Progress on medium", confidence=0.8,
            ),
        ]

        synth = Synthesizer(drafts_dir=tmp_path)
        changelog = synth.synthesize(signals, date="2026-03-01", correlator=correlator)

        # Entries should be ordered: Critical (5), Medium (3), Low (1)
        initiatives_in_order = []
        for e in changelog.entries:
            if e.initiative not in initiatives_in_order:
                initiatives_in_order.append(e.initiative)

        assert initiatives_in_order == ["Critical Project", "Medium Project", "Low Priority"]

    def test_without_correlator_alphabetical(self, tmp_path):
        """Without correlator, initiatives are sorted by default weight then alphabetically."""
        signals = [
            Signal(
                source="freetext", timestamp="2026-03-01T10:00:00Z",
                raw_text="x", initiatives=["Zebra"],
                signal_type="progress", detail="Zebra work", confidence=0.8,
            ),
            Signal(
                source="freetext", timestamp="2026-03-01T10:00:00Z",
                raw_text="x", initiatives=["Alpha"],
                signal_type="progress", detail="Alpha work", confidence=0.8,
            ),
        ]

        synth = Synthesizer(drafts_dir=tmp_path)
        changelog = synth.synthesize(signals, date="2026-03-01")

        initiatives_in_order = []
        for e in changelog.entries:
            if e.initiative not in initiatives_in_order:
                initiatives_in_order.append(e.initiative)

        assert initiatives_in_order == ["Alpha", "Zebra"]


class TestTemporalDedup:
    """Tests for temporal deduplication (Pass 4)."""

    def test_keeps_most_recent(self, tmp_path):
        """Within same initiative+type, keep most recent for same workflow stage."""
        signals = [
            Signal(
                source="freetext", timestamp="2026-03-01T10:00:00Z",
                raw_text="x", initiatives=["Alpha"],
                signal_type="progress", detail="Review meeting scheduled for Monday",
                confidence=0.8,
            ),
            Signal(
                source="freetext", timestamp="2026-03-03T10:00:00Z",
                raw_text="x", initiatives=["Alpha"],
                signal_type="progress", detail="Review meeting scheduled for Wednesday",
                confidence=0.8,
            ),
        ]

        synth = Synthesizer(drafts_dir=tmp_path)
        changelog = synth.synthesize(signals, date="2026-03-03")

        alpha_progress = [e for e in changelog.entries
                          if e.initiative == "Alpha" and e.signal_type == "progress"]
        assert len(alpha_progress) == 1
        assert "Wednesday" in alpha_progress[0].detail

    def test_different_stages_kept(self, tmp_path):
        """Signals describing different workflow stages are both kept."""
        signals = [
            Signal(
                source="freetext", timestamp="2026-03-01T10:00:00Z",
                raw_text="x", initiatives=["Alpha"],
                signal_type="progress", detail="Started implementation of feature X",
                confidence=0.8,
            ),
            Signal(
                source="freetext", timestamp="2026-03-03T10:00:00Z",
                raw_text="x", initiatives=["Alpha"],
                signal_type="progress", detail="Completed testing of module Y with full coverage",
                confidence=0.8,
            ),
        ]

        synth = Synthesizer(drafts_dir=tmp_path)
        changelog = synth.synthesize(signals, date="2026-03-03")

        alpha_progress = [e for e in changelog.entries
                          if e.initiative == "Alpha" and e.signal_type == "progress"]
        assert len(alpha_progress) == 2


class TestBeneficiaryTagging:
    """Tests for beneficiary tagging on action items."""

    def test_action_items_get_beneficiary_tag(self, tmp_path):
        """Action items include '— Benefits: owners' when correlator is provided."""
        from axiom.extensions.builtins.signals.correlator import Correlator, Initiative

        correlator = Correlator.__new__(Correlator)
        correlator.config_dir = tmp_path
        correlator.people = []
        correlator.initiatives = [
            Initiative(id="1", name="Alpha", owners=["Smith", "Jones"], strategic_weight=3),
        ]

        signals = [
            Signal(
                source="freetext", timestamp="2026-03-01T10:00:00Z",
                raw_text="x", initiatives=["Alpha"],
                signal_type="action_item", detail="Schedule review meeting",
                confidence=0.8,
            ),
        ]

        synth = Synthesizer(drafts_dir=tmp_path)
        changelog = synth.synthesize(signals, date="2026-03-01", correlator=correlator)
        path = synth.write_weekly_summary(changelog, correlator=correlator)

        content = path.read_text()
        assert "Benefits: Smith, Jones" in content

    def test_progress_no_beneficiary_tag(self, tmp_path):
        """Progress entries do NOT get beneficiary tags."""
        from axiom.extensions.builtins.signals.correlator import Correlator, Initiative

        correlator = Correlator.__new__(Correlator)
        correlator.config_dir = tmp_path
        correlator.people = []
        correlator.initiatives = [
            Initiative(id="1", name="Alpha", owners=["Smith"], strategic_weight=3),
        ]

        signals = [
            Signal(
                source="freetext", timestamp="2026-03-01T10:00:00Z",
                raw_text="x", initiatives=["Alpha"],
                signal_type="progress", detail="Completed feature X",
                confidence=0.8,
            ),
        ]

        synth = Synthesizer(drafts_dir=tmp_path)
        changelog = synth.synthesize(signals, date="2026-03-01", correlator=correlator)
        path = synth.write_weekly_summary(changelog, correlator=correlator)

        content = path.read_text()
        assert "Benefits:" not in content


class TestWeeklySummaryAnnotations:
    """Tests for initiative annotations in weekly summary."""

    def test_paused_initiative_annotation(self, tmp_path):
        """Initiatives with pause_reason show '(Paused: reason)'."""
        from axiom.extensions.builtins.signals.correlator import Correlator, Initiative

        correlator = Correlator.__new__(Correlator)
        correlator.config_dir = tmp_path
        correlator.people = []
        correlator.initiatives = [
            Initiative(
                id="1", name="MIT Loop",
                pause_reason="MIT system offline since 2024",
                strategic_weight=2,
            ),
        ]

        signals = [
            Signal(
                source="gitlab_diff", timestamp="2026-03-01T10:00:00Z",
                raw_text="x", initiatives=["MIT Loop"],
                signal_type="status_change", detail="No recent activity",
                confidence=1.0,
            ),
        ]

        synth = Synthesizer(drafts_dir=tmp_path)
        changelog = synth.synthesize(signals, date="2026-03-01", correlator=correlator)
        path = synth.write_weekly_summary(changelog, correlator=correlator)

        content = path.read_text()
        assert "Paused: MIT system offline since 2024" in content

    def test_stale_initiative_needs_attention(self, tmp_path):
        """Stale initiatives without pause_reason show '(Needs attention)'."""
        from axiom.extensions.builtins.signals.correlator import Correlator, Initiative

        correlator = Correlator.__new__(Correlator)
        correlator.config_dir = tmp_path
        correlator.people = []
        correlator.initiatives = [
            Initiative(id="1", name="ERCOT DT", status="Stale", strategic_weight=2),
        ]

        signals = [
            Signal(
                source="gitlab_diff", timestamp="2026-03-01T10:00:00Z",
                raw_text="x", initiatives=["ERCOT DT"],
                signal_type="status_change", detail="No recent commits",
                confidence=1.0,
            ),
        ]

        synth = Synthesizer(drafts_dir=tmp_path)
        changelog = synth.synthesize(signals, date="2026-03-01", correlator=correlator)
        path = synth.write_weekly_summary(changelog, correlator=correlator)

        content = path.read_text()
        assert "Needs attention" in content

    def test_structural_only_flag(self, tmp_path):
        """Initiatives with only structural signals get needs-context flag."""
        from axiom.extensions.builtins.signals.correlator import Correlator, Initiative

        correlator = Correlator.__new__(Correlator)
        correlator.config_dir = tmp_path
        correlator.people = []
        correlator.initiatives = [
            Initiative(id="1", name="SyTH", status="Active", strategic_weight=3),
        ]

        signals = [
            Signal(
                source="gitlab_diff", timestamp="2026-03-01T10:00:00Z",
                raw_text="x", initiatives=["SyTH"],
                signal_type="progress", detail="3 commits this week",
                confidence=1.0,
            ),
        ]

        synth = Synthesizer(drafts_dir=tmp_path)
        changelog = synth.synthesize(signals, date="2026-03-01", correlator=correlator)
        path = synth.write_weekly_summary(changelog, correlator=correlator)

        content = path.read_text()
        assert "Structural signals only" in content

    def test_blocker_section_in_summary(self, tmp_path):
        """Active blockers section appears before initiative sections."""
        from axiom.extensions.builtins.signals.blocker_tracker import BlockerTracker

        tracker = BlockerTracker(state_path=tmp_path / "state" / "blocker_state.json")
        blocker_signals = [
            Signal(
                source="freetext", timestamp="2026-03-01T10:00:00Z",
                raw_text="x", people=["Alice"],
                initiatives=["Alpha", "Beta"],
                signal_type="blocker", detail="API key needed",
                confidence=0.8,
            ),
        ]
        tracker.update(blocker_signals)

        signals = [
            Signal(
                source="freetext", timestamp="2026-03-01T10:00:00Z",
                raw_text="x", initiatives=["Alpha"],
                signal_type="progress", detail="Some progress",
                confidence=0.8,
            ),
        ]

        synth = Synthesizer(drafts_dir=tmp_path)
        changelog = synth.synthesize(signals, date="2026-03-01")
        path = synth.write_weekly_summary(changelog, blocker_tracker=tracker)

        content = path.read_text()
        # Blocker section should exist
        assert "## Active Blockers" in content
        assert "API key needed" in content
        # Blocker section should come before initiative sections
        blocker_pos = content.index("## Active Blockers")
        alpha_pos = content.index("## Alpha")
        assert blocker_pos < alpha_pos
