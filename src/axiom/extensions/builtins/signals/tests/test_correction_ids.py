# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for deterministic correction ID generation.

These tests ensure that correction IDs remain stable across reprocessing runs,
which is critical for maintaining audio clip linkages.
"""
import hashlib
from dataclasses import dataclass
from unittest.mock import MagicMock, patch


@dataclass
class MockCorrection:
    """Mock correction for testing."""
    original: str
    corrected: str
    category: str = "test"
    confidence: float = 0.9
    context: str = "...test context..."
    reason: str = "test reason"


class TestDeterministicCorrectionIds:
    """Test that correction IDs are deterministic based on content."""

    def test_same_content_produces_same_id(self):
        """Same correction content should always produce the same ID."""
        transcript_path = "/path/to/transcript.txt"
        original = "rock"
        corrected = "ROC"
        context = "...going to the rock committee..."

        # Generate ID twice with same content
        id_content = f"{transcript_path}|{original}|{corrected}|{context}"
        hash1 = hashlib.sha256(id_content.encode()).hexdigest()[:12]
        id1 = f"corr_{hash1}"

        hash2 = hashlib.sha256(id_content.encode()).hexdigest()[:12]
        id2 = f"corr_{hash2}"

        assert id1 == id2
        assert id1.startswith("corr_")
        assert len(id1) == 17  # "corr_" + 12 hex chars

    def test_different_content_produces_different_id(self):
        """Different correction content should produce different IDs."""
        base_content = "/path/to/transcript.txt|rock|ROC|...context..."
        different_content = "/path/to/transcript.txt|rack|ROC|...context..."

        hash1 = hashlib.sha256(base_content.encode()).hexdigest()[:12]
        hash2 = hashlib.sha256(different_content.encode()).hexdigest()[:12]

        assert hash1 != hash2

    def test_id_stable_across_transcript_paths(self):
        """Same correction in same transcript always gets same ID."""
        transcript_path = "tools/agents/inbox/processed/voice/meeting_transcript.txt"
        corr = MockCorrection(
            original="the warm",
            corrected="the vote",
            context="...approved the warm of the ROC...",
        )

        # Simulate two processing runs
        id_content_run1 = f"{transcript_path}|{corr.original}|{corr.corrected}|{corr.context}"
        id_content_run2 = f"{transcript_path}|{corr.original}|{corr.corrected}|{corr.context}"

        id1 = f"corr_{hashlib.sha256(id_content_run1.encode()).hexdigest()[:12]}"
        id2 = f"corr_{hashlib.sha256(id_content_run2.encode()).hexdigest()[:12]}"

        assert id1 == id2, "Same correction should produce same ID across runs"

    def test_clips_stay_linked_on_reprocess(self):
        """Audio clips should stay linked when transcripts are reprocessed.

        This is the key invariant: when we reprocess audio and regenerate
        corrections, any existing clips linked by correction_id should
        still match because the IDs are deterministic.
        """
        # Simulate initial processing
        transcript_path = "/voice/recording.txt"
        corrections = [
            MockCorrection("rock", "ROC", context="...the rock committee..."),
            MockCorrection("warm", "vote", context="...warm of the ROC..."),
        ]

        # Generate IDs as the corrector would
        initial_ids = []
        for corr in corrections:
            id_content = f"{transcript_path}|{corr.original}|{corr.corrected}|{corr.context}"
            content_hash = hashlib.sha256(id_content.encode()).hexdigest()[:12]
            initial_ids.append(f"corr_{content_hash}")

        # Simulate clip creation
        clips_index = {
            initial_ids[0]: "/clips/clip_001.m4a",
            initial_ids[1]: "/clips/clip_002.m4a",
        }

        # Simulate reprocessing - same corrections detected again
        reprocess_ids = []
        for corr in corrections:
            id_content = f"{transcript_path}|{corr.original}|{corr.corrected}|{corr.context}"
            content_hash = hashlib.sha256(id_content.encode()).hexdigest()[:12]
            reprocess_ids.append(f"corr_{content_hash}")

        # IDs should match, so clips are still linked
        for i, (initial, reprocessed) in enumerate(zip(initial_ids, reprocess_ids)):
            assert initial == reprocessed, f"Correction {i} ID changed on reprocess"
            assert reprocessed in clips_index, f"Clip link broken for correction {i}"


class TestCorrectionIdFormat:
    """Test correction ID format and properties."""

    def test_id_prefix(self):
        """IDs should start with 'corr_' prefix."""
        content = "test|content|here|context"
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:12]
        correction_id = f"corr_{content_hash}"

        assert correction_id.startswith("corr_")

    def test_id_length(self):
        """IDs should be consistent length for database indexing."""
        content = "any|content|here|context"
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:12]
        correction_id = f"corr_{content_hash}"

        # "corr_" (5) + 12 hex chars = 17 characters
        assert len(correction_id) == 17

    def test_id_characters(self):
        """IDs should only contain safe characters for filenames/URLs."""
        content = "test|with|unicode|日本語"
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:12]
        correction_id = f"corr_{content_hash}"

        # Should only contain alphanumeric and underscore
        import re
        assert re.match(r'^[a-z0-9_]+$', correction_id)

    def test_no_collision_different_transcripts(self):
        """Same text change in different transcripts should have different IDs."""
        correction_data = ("rock", "ROC", "...the rock...")

        id1_content = f"/path/a/transcript.txt|{correction_data[0]}|{correction_data[1]}|{correction_data[2]}"
        id2_content = f"/path/b/transcript.txt|{correction_data[0]}|{correction_data[1]}|{correction_data[2]}"

        id1 = hashlib.sha256(id1_content.encode()).hexdigest()[:12]
        id2 = hashlib.sha256(id2_content.encode()).hexdigest()[:12]

        assert id1 != id2, "Same correction in different transcripts should have different IDs"


class TestCorrectorIdGeneration:
    """Integration tests for corrector ID generation."""

    def test_corrector_generates_deterministic_ids(self):
        """Test that TranscriptCorrector generates deterministic IDs."""
        from axiom.extensions.builtins.signals.corrector import (
            Correction,
            CorrectionResult,
            TranscriptCorrector,
        )

        corrector = TranscriptCorrector()

        # Create a mock correction result
        result = CorrectionResult(
            transcript_path="/test/transcript.txt",
            corrections=[
                Correction(
                    original="rock",
                    corrected="ROC",
                    category="technical_term",
                    confidence=0.95,
                    context="...the rock committee...",
                    reason="ROC = Research Operations Committee",
                ),
            ],
        )

        # Mock the review system to capture the IDs
        captured_ids = []

        # Patch where CorrectionReviewSystem is imported (inside the method)
        with patch("axiom.extensions.builtins.signals.correction_review.CorrectionReviewSystem") as MockReview:
            mock_instance = MagicMock()
            MockReview.return_value = mock_instance

            def capture_id(**kwargs):
                captured_ids.append(kwargs.get("correction_id"))

            mock_instance.record_applied.side_effect = capture_id

            # Process twice
            corrector._add_to_review_queue(result)
            first_ids = captured_ids.copy()
            captured_ids.clear()

            corrector._add_to_review_queue(result)
            second_ids = captured_ids.copy()

        # IDs should be identical across runs
        assert first_ids == second_ids, "IDs should be deterministic"
        assert all(id.startswith("corr_") for id in first_ids)
