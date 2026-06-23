# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for guided correction review with audio clips and downstream propagation.

Golden copy test fixtures are in tests/sense/fixtures/audio_clips/
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Test fixture paths
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "audio_clips"
GOLDEN_CLIP = FIXTURES_DIR / "golden_correction_clip.m4a"
GOLDEN_METADATA = FIXTURES_DIR / "golden_correction_metadata.json"


class TestGoldenCopyFixtures:
    """Tests using golden copy audio clips."""

    @pytest.mark.skipif(
        not GOLDEN_CLIP.exists(),
        reason="Golden clip fixture not available (binary asset not committed)",
    )
    def test_golden_clip_exists(self):
        """Verify golden copy test fixture exists."""
        assert GOLDEN_CLIP.exists(), f"Golden clip missing: {GOLDEN_CLIP}"
        assert GOLDEN_CLIP.stat().st_size > 10000, "Golden clip too small"

    def test_golden_metadata_valid(self):
        """Verify golden copy metadata is valid."""
        assert GOLDEN_METADATA.exists()

        data = json.loads(GOLDEN_METADATA.read_text())

        assert "clips" in data
        assert len(data["clips"]) > 0

        clip = data["clips"][0]
        assert "file" in clip
        assert "expected_correction" in clip
        assert clip["expected_correction"]["original"] == "Covertilla"
        assert clip["expected_correction"]["corrected"] == "Cobra-TF"


class TestGuidedCorrectionReview:
    """Tests for GuidedCorrectionReview class."""

    @pytest.fixture
    def temp_dirs(self, tmp_path):
        """Create temporary directories for test isolation."""
        clips_dir = tmp_path / "audio_clips"
        clips_dir.mkdir()

        corrections_dir = tmp_path / "corrections"
        corrections_dir.mkdir()

        return {
            "clips_dir": clips_dir,
            "corrections_dir": corrections_dir,
        }

    @pytest.fixture
    def guided_review(self, temp_dirs):
        """Create isolated GuidedCorrectionReview instance."""
        from axiom.extensions.builtins.signals.correction_review_guided import (
            GuidedCorrectionReview,
        )

        # Patch the paths to use temp directories
        with patch.object(
            GuidedCorrectionReview,
            "__init__",
            lambda self: None,
        ):
            review = GuidedCorrectionReview()
            review._state = MagicMock()
            review._state.pending_clips = []
            review._state.deferred_until = ""
            review._state.deferral_count = 0
            review._clips = {}

            # Patch module-level constants for testing
            import axiom.extensions.builtins.signals.correction_review_guided as guided_module
            self._original_clips_dir = guided_module.CLIPS_DIR
            guided_module.CLIPS_DIR = temp_dirs["clips_dir"]

            return review

    def test_should_prompt_no_corrections(self, guided_review):
        """Test that prompt check returns False when no corrections pending."""
        guided_review.get_pending_corrections = MagicMock(return_value=[])

        should, msg = guided_review.should_prompt_review()

        assert not should
        assert "No corrections" in msg

    def test_defer_review(self, guided_review):
        """Test deferring review updates state."""
        guided_review._save_state = MagicMock()

        msg = guided_review.defer_review(hours=12)

        assert "12 hours" in msg
        assert guided_review._state.deferral_count == 1

    def test_extract_audio_clip_no_ffmpeg(self, temp_dirs):
        """Test clip extraction falls back when ffmpeg unavailable."""
        from axiom.extensions.builtins.signals.correction_review_guided import (
            GuidedCorrectionReview,
        )

        # Create a real instance for this test
        with patch("axiom.extensions.builtins.signals.correction_review_guided.CLIPS_DIR", temp_dirs["clips_dir"]):
            with patch("axiom.extensions.builtins.signals.correction_review_guided.REVIEW_STATE_FILE", temp_dirs["corrections_dir"] / "state.json"):
                with patch("shutil.which", return_value=None):  # No ffmpeg
                    review = GuidedCorrectionReview()

                    # Create a fake source audio
                    fake_audio = temp_dirs["clips_dir"] / "source.m4a"
                    fake_audio.write_bytes(b"fake audio content")

                    clip = review.extract_audio_clip(
                        correction_id="test_corr_123",
                        source_audio_path=str(fake_audio),
                        start_time_sec=0,
                        end_time_sec=5,
                        transcript_segment="test transcript",
                    )

                    assert clip is not None
                    assert clip.clip_path == ""  # No extraction when ffmpeg missing
                    assert clip.source_audio_path == str(fake_audio)


class TestCorrectionPropagation:
    """Tests for downstream correction propagation."""

    @pytest.fixture
    def temp_propagation_dir(self, tmp_path):
        """Create temporary propagation directory."""
        prop_dir = tmp_path / "corrections"
        prop_dir.mkdir()
        return prop_dir

    @pytest.fixture
    def propagator(self, temp_propagation_dir):
        """Create isolated CorrectionPropagator."""
        from axiom.extensions.builtins.signals.correction_propagation import (
            CorrectionPropagator,
        )

        with patch("axiom.extensions.builtins.signals.correction_propagation.PROPAGATION_QUEUE", temp_propagation_dir / "queue.json"):
            with patch("axiom.extensions.builtins.signals.correction_propagation.PROPAGATION_LOG", temp_propagation_dir / "log.jsonl"):
                with patch("axiom.extensions.builtins.signals.correction_propagation.USER_GLOSSARY", temp_propagation_dir / "glossary.json"):
                    return CorrectionPropagator()

    def test_propagate_approval_creates_jobs(self, propagator, temp_propagation_dir):
        """Test that approval creates expected propagation jobs."""
        result = propagator.propagate_approval(
            correction_id="test_123",
            original="covertilla",
            corrected="Cobra-TF",
            category="technical_term",
            signal_ids=["sig_001"],
            clip_id="clip_001",
            confirmed_by="test_user",
        )

        assert result.correction_id == "test_123"
        assert result.action == "approval"
        assert result.jobs_created >= 2  # At least glossary + RAG
        assert result.glossary_updated

    def test_propagate_approval_updates_glossary(self, propagator, temp_propagation_dir):
        """Test that approval adds term to glossary."""
        glossary_path = temp_propagation_dir / "glossary.json"

        with patch("axiom.extensions.builtins.signals.correction_propagation.USER_GLOSSARY", glossary_path):
            propagator.propagate_approval(
                correction_id="test_456",
                original="covertilla",
                corrected="Cobra-TF",
                category="technical_term",
                signal_ids=[],
                confirmed_by="ben",
            )

            assert glossary_path.exists()
            glossary = json.loads(glossary_path.read_text())
            assert "terms" in glossary
            assert glossary["terms"].get("covertilla") == "Cobra-TF"

    def test_propagate_rejection_queues_resynthesis(self, propagator):
        """Test that rejection queues re-synthesis jobs."""
        result = propagator.propagate_rejection(
            correction_id="test_789",
            original="any up",
            suggested="N-UP",  # Wrong
            actual_correct="RUP",  # Correct
            category="acronym",
            signal_ids=["sig_002"],
            published_endpoints=["changelog_2026-02-24.md"],
            flagged_by="ben",
            reason="RUP is the research program, not R-UP",
        )

        assert result.action == "rejection"
        assert result.jobs_created >= 3  # Glossary add, remove, resynthesis, notification

    def test_propagation_stats(self, propagator):
        """Test propagation statistics."""
        # Create some jobs
        propagator.propagate_approval(
            correction_id="stat_test_1",
            original="test",
            corrected="TEST",
            category="acronym",
            signal_ids=[],
            confirmed_by="test",
        )

        stats = propagator.get_propagation_stats()

        assert stats["total_jobs"] > 0
        assert "by_status" in stats
        assert "by_type" in stats


class TestDownstreamUpdates:
    """Tests verifying downstream systems are updated correctly."""

    def test_glossary_add_handler(self, tmp_path):
        """Test glossary add handler creates correct entries."""
        from axiom.extensions.builtins.signals.correction_propagation import (
            CorrectionPropagator,
            PropagationJob,
            PropagationType,
        )

        glossary_path = tmp_path / "glossary.json"

        with patch("axiom.extensions.builtins.signals.correction_propagation.USER_GLOSSARY", glossary_path):
            propagator = CorrectionPropagator()

            job = PropagationJob(
                id="test_job_1",
                correction_id="corr_1",
                propagation_type=PropagationType.GLOSSARY_ADD.value,
                payload={
                    "original": "flybe",
                    "corrected": "FLiBe",
                    "category": "technical_term",
                },
            )

            success = propagator._handle_glossary_add(job)

            assert success
            glossary = json.loads(glossary_path.read_text())
            assert glossary["terms"]["flybe"] == "FLiBe"

    def test_glossary_person_category(self, tmp_path):
        """Test that person names go to people section."""
        from axiom.extensions.builtins.signals.correction_propagation import (
            CorrectionPropagator,
            PropagationJob,
            PropagationType,
        )

        glossary_path = tmp_path / "glossary.json"

        with patch("axiom.extensions.builtins.signals.correction_propagation.USER_GLOSSARY", glossary_path):
            propagator = CorrectionPropagator()

            job = PropagationJob(
                id="test_job_2",
                correction_id="corr_2",
                propagation_type=PropagationType.GLOSSARY_ADD.value,
                payload={
                    "original": "so ha",
                    "corrected": "Soja",
                    "category": "person_name",
                },
            )

            success = propagator._handle_glossary_add(job)

            assert success
            glossary = json.loads(glossary_path.read_text())
            assert glossary["people"]["so ha"] == "Soja"

    def test_notification_logged(self, tmp_path):
        """Test notifications are written to log."""
        import axiom.extensions.builtins.signals.correction_propagation as prop_module
        from axiom.extensions.builtins.signals.correction_propagation import (
            CorrectionPropagator,
            PropagationJob,
            PropagationType,
        )

        # Create inbox dir
        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir(exist_ok=True)

        with patch.object(prop_module, "_RUNTIME_DIR", tmp_path):
            propagator = CorrectionPropagator()

            job = PropagationJob(
                id="test_notif",
                correction_id="corr_notif",
                propagation_type=PropagationType.NOTIFICATION.value,
                payload={
                    "type": "correction_approved",
                    "message": "Test notification",
                    "importance": "low",
                },
            )

            success = propagator._handle_notification(job)

            assert success

            log_path = inbox_dir / "notifications.jsonl"
            assert log_path.exists()


class TestPatternMatching:
    """Tests for auto-confirm matching patterns feature."""

    @pytest.fixture
    def system_with_patterns(self, tmp_path):
        """Create CorrectionReviewSystem with test corrections."""
        from axiom.extensions.builtins.signals.correction_review import (
            CorrectionReviewSystem,
        )

        # Create isolated corrections dir
        corrections_dir = tmp_path / "corrections"
        corrections_dir.mkdir()

        system = CorrectionReviewSystem(corrections_dir=corrections_dir)

        # Add test corrections - some with matching patterns
        test_corrections = [
            # Pattern 1: "warm of the rock" → "majority of the ROC" (3 instances)
            ("corr_1", "warm of the rock", "majority of the ROC", "term_correction"),
            ("corr_2", "warm of the rock", "majority of the ROC", "term_correction"),
            ("corr_3", "warm of the rock", "majority of the ROC", "term_correction"),
            # Pattern 2: "rock chairperson" → "ROC chairperson" (2 instances)
            ("corr_4", "rock chairperson", "ROC chairperson", "term_correction"),
            ("corr_5", "rock chairperson", "ROC chairperson", "term_correction"),
            # Unique correction
            ("corr_6", "unique error", "unique fix", "term_correction"),
        ]

        for cid, original, corrected, category in test_corrections:
            system.record_applied(
                correction_id=cid,
                transcript_path="/test/transcript.md",
                original=original,
                corrected=corrected,
                category=category,
                confidence=0.9,
                context=f"test context for {original}",
                reason="test reason",
            )

        return system

    def test_get_matching_pattern_corrections(self, system_with_patterns):
        """Test finding corrections with same pattern."""
        system = system_with_patterns

        # Find matches for pattern 1 (should find 2 others, excluding corr_1)
        matches = system.get_matching_pattern_corrections(
            original="warm of the rock",
            corrected="majority of the ROC",
            exclude_id="corr_1",
        )

        assert len(matches) == 2
        match_ids = {m.id for m in matches}
        assert match_ids == {"corr_2", "corr_3"}

    def test_batch_confirm_pattern(self, system_with_patterns):
        """Test batch confirming all instances of a pattern."""
        system = system_with_patterns

        # First confirm corr_1 manually
        system.confirm_correct("corr_1", confirmed_by="test_user")

        # Then batch confirm siblings
        confirmed = system.batch_confirm_pattern(
            original="warm of the rock",
            corrected="majority of the ROC",
            confirmed_by="test_user",
            exclude_id="corr_1",
        )

        assert len(confirmed) == 2

        # Verify all are now confirmed
        pending = system.get_unfeedback_corrections()
        pending_ids = {c.id for c in pending}

        # corr_1, corr_2, corr_3 should all be confirmed
        assert "corr_1" not in pending_ids
        assert "corr_2" not in pending_ids
        assert "corr_3" not in pending_ids

        # Other patterns should still be pending
        assert "corr_4" in pending_ids
        assert "corr_5" in pending_ids
        assert "corr_6" in pending_ids

    def test_no_auto_confirm_different_patterns(self, system_with_patterns):
        """Test that different patterns are not auto-confirmed."""
        system = system_with_patterns

        # Confirm pattern 1
        system.batch_confirm_pattern(
            original="warm of the rock",
            corrected="majority of the ROC",
            confirmed_by="test_user",
        )

        # Pattern 2 should not be affected
        pending = system.get_unfeedback_corrections()
        pending_originals = {c.original for c in pending}

        assert "rock chairperson" in pending_originals


# Placeholder for actual correction term test - user will provide
class TestActualCorrectionTerm:
    """Tests using actual correction terms identified by user.

    TODO: User will identify a specific correction term for golden testing.
    """

    @pytest.mark.skip(reason="Waiting for user to identify actual correction term")
    def test_specific_correction_term(self):
        """Test a specific correction identified from real audio."""
        pass
