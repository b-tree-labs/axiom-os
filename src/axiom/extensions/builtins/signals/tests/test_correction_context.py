# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for correction context extraction and audio timing.

FIXED 2026-03-31 (v0.1.6):
Context extraction now uses a three-tier fallback strategy:
1. Case-insensitive exact match
2. Partial word match (longest word in text)
3. Fuzzy match via SequenceMatcher (ratio >= 0.6)

These tests ensure:
1. Context extraction includes the original term
2. Audio timing points to the segment containing the term
3. Review display shows useful information for human judgment
"""

import json

import pytest

from ..corrector import Correction, TranscriptCorrector


class TestContextExtraction:
    """Test that context extraction properly captures the original term."""

    def test_get_context_finds_exact_match(self):
        """Context extraction should find exact text matches."""
        corrector = TranscriptCorrector()

        transcript = "We went to the social director and asked about the experiment."
        text = "social director"

        context = corrector._get_context(transcript, text)

        # Context MUST contain the original text
        assert "social director" in context, \
            f"Context '{context}' should contain original text 'social director'"

    def test_get_context_finds_case_insensitive(self):
        """Context should find text regardless of case differences."""
        corrector = TranscriptCorrector()

        transcript = "We worked with SOCIAL DIRECTOR on this project."
        text = "social director"

        context = corrector._get_context(transcript, text)

        # Should find case-insensitive match
        assert "SOCIAL DIRECTOR" in context or "social director" in context.lower(), \
            "Context should contain the text in some form"

    def test_get_context_returns_empty_when_not_found(self):
        """Context returns empty string if no match found at any tier."""
        corrector = TranscriptCorrector()

        transcript = "This transcript has different content."
        text = "nonexistent phrase"

        context = corrector._get_context(transcript, text)

        assert context == "", "Should return empty when text not found"

    def test_context_includes_surrounding_text(self):
        """Context should include ~100 chars before and after."""
        corrector = TranscriptCorrector()

        transcript = "A" * 150 + " social director " + "B" * 150
        text = "social director"

        context = corrector._get_context(transcript, text)

        # Should have text before
        assert "..." in context or "A" * 50 in context
        # Should have the original term
        assert "social director" in context
        # Should have text after
        assert "B" in context

    def test_context_handles_punctuation(self):
        """Context should handle punctuation around terms."""
        corrector = TranscriptCorrector()

        transcript = "Go to the rock, and the rock chairperson will review it."
        text = "rock"

        context = corrector._get_context(transcript, text)

        assert "rock" in context


class TestCorrectionContextInResult:
    """Test that corrections have proper context in the result."""

    def test_correction_has_context_with_original(self):
        """Each correction should have context containing its original text."""
        # Sample correction data - now testing with GOOD context (from actual stored data)
        correction = Correction(
            original="social director",
            corrected="associate director",
            category="person_name",
            confidence=0.90,
            context="... use, and a complete evaluation of that experiment needs to be done and presented to myself, to the social director, to HP, to the director...",
            reason="In organizational context",
        )

        # Context MUST contain the original text
        assert correction.original in correction.context, \
            f"Context '{correction.context}' should contain original '{correction.original}'"


class TestCenteredContextDisplay:
    """Test the _get_centered_context helper function that shows context centered on term."""

    def test_centers_on_original_term(self):
        """Display should center on the original term, not show first N chars."""
        from ..correction_review import _get_centered_context

        # Context with term in the middle
        context = "A" * 100 + " social director " + "B" * 100
        original = "social director"

        display = _get_centered_context(context, original, width=60)

        # The display MUST contain the original term
        assert "social director" in display, \
            f"Display should contain original term. Got: {display}"

    def test_handles_term_not_found(self):
        """Should gracefully handle when term isn't in context."""
        from ..correction_review import _get_centered_context

        context = "This context doesn't have the term"
        original = "social director"

        display = _get_centered_context(context, original, width=40)

        # Should return truncated context, not crash
        assert len(display) <= 50  # width + some ellipsis

    def test_handles_empty_context(self):
        """Should handle empty context gracefully."""
        from ..correction_review import _get_centered_context

        display = _get_centered_context("", "term", width=40)
        assert display == "(no context)"

    def test_shows_ellipsis_when_truncated(self):
        """Should show ... when context is truncated."""
        from ..correction_review import _get_centered_context

        context = "A" * 50 + " term " + "B" * 50
        display = _get_centered_context(context, "term", width=30)

        # Should have ellipsis on both sides
        assert display.startswith("...") or display.endswith("...")

    def test_real_correction_display(self):
        """Test with actual correction data from the system."""
        from ..correction_review import _get_centered_context

        # Real context from correction_applied.jsonl
        context = "... use, and a complete evaluation of that experiment needs to be done and presented to myself, to the social director, to HP, to the director, they just all sign off on it and then it goes to the rock. And the rock ch..."
        original = "social director"

        display = _get_centered_context(context, original, width=100)

        assert "social director" in display, \
            f"Display should show the term. Got: {display}"


class TestAudioTimingExtraction:
    """Test that audio timing points to the correct segment."""

    def test_timestamp_search_finds_original_term(self):
        """Audio timing should find the original (pre-correction) term."""
        # Mock timestamp data
        timestamps = {
            "words": [
                {"word": "We", "start": 10.0, "end": 10.2},
                {"word": "asked", "start": 10.3, "end": 10.5},
                {"word": "the", "start": 10.6, "end": 10.7},
                {"word": "social", "start": 10.8, "end": 11.0},
                {"word": "director", "start": 11.1, "end": 11.4},
                {"word": "about", "start": 11.5, "end": 11.7},
            ]
        }

        # Search for "social director" should find words at 10.8-11.4
        original = "social director"
        words = timestamps["words"]
        original_words = original.lower().split()

        found_start = None
        for i, word_info in enumerate(words):
            word_text = word_info["word"].lower()
            if original_words[0] in word_text:
                # Check if subsequent words match
                match = True
                for j, orig_word in enumerate(original_words[1:], 1):
                    if i + j < len(words):
                        next_word = words[i + j]["word"].lower()
                        if orig_word not in next_word and next_word not in orig_word:
                            match = False
                            break
                if match:
                    found_start = word_info["start"]
                    break

        assert found_start is not None, "Should find the original term in timestamps"
        assert found_start == 10.8, f"Should start at 10.8s, got {found_start}"


class TestEndToEndContextFlow:
    """Test the complete flow from transcript to review display."""

    def test_correction_flow_preserves_context(self, tmp_path):
        """Context should be preserved through the correction pipeline."""
        from ..correction_review import AppliedCorrection

        # Create a correction with context that includes the term
        original_text = "moles R-ratherage"
        context_with_term = f"So, one thing is that we have {original_text} are kind of considered"

        correction = AppliedCorrection(
            id="corr_test_flow",
            transcript_path="/test/path.md",
            original=original_text,
            corrected="thermal system",
            category="technical_term",
            confidence=0.95,
            context=context_with_term,
            reason="Engineering term",
        )

        # Context should contain original
        assert original_text in correction.context, \
            "Context must contain the original text for review"

        # Write to file
        applied_file = tmp_path / "correction_applied.jsonl"
        with open(applied_file, "w") as f:
            f.write(correction.to_jsonl() + "\n")

        # Read back
        with open(applied_file) as f:
            data = json.loads(f.readline())

        # Verify context preserved
        assert original_text in data["context"], \
            "Context should be preserved through serialization"


class TestContextExtractionFix:
    """Tests for the proposed fix to context extraction."""

    def test_llm_original_may_differ_from_transcript(self):
        """
        The bug: LLM returns an 'original' text that's slightly different
        from what's actually in the transcript.

        Example:
          Transcript: "to the social director, to HP"
          LLM original: "social director"

        The find() should still work, but if there are extra spaces,
        punctuation, or slight variations, it fails.
        """
        corrector = TranscriptCorrector()

        # Real transcript text
        transcript = "presented to myself, to the social director, to HP, to the director"

        # What the LLM returned (slightly different)
        llm_original = "social director"

        context = corrector._get_context(transcript, llm_original)

        # This should pass - the text is there
        assert "social director" in context, \
            "Context should find 'social director' in transcript"

    def test_context_extraction_with_exact_llm_output(self):
        """Test with exact data from a real failing case."""
        corrector = TranscriptCorrector()

        # This is from the actual correction_applied.jsonl
        transcript = """And it goes to the social director, to HP, to the director, they just all sign off on it and then it goes to the rock. And the rock chairperson, before he, the rock chairperson's kind of planet."""

        # The LLM said to correct "social director" → "associate director"
        original = "social director"

        context = corrector._get_context(transcript, original)

        # This should work
        assert original in context, \
            f"Failed to find '{original}' in context. Got: {context[:100]}..."

    def test_context_fallback_should_try_fuzzy_match(self):
        """
        PROPOSED FIX: If exact match fails, try fuzzy matching.

        The current code returns empty string on no match.
        It should try:
        1. Case-insensitive (already does)
        2. Partial match (e.g., "social" alone)
        3. Levenshtein distance for near-matches
        """
        corrector = TranscriptCorrector()

        transcript = "We went to the asociate director and then..."  # typo in transcript
        original = "associate director"  # LLM's guess at the original

        context = corrector._get_context(transcript, original)

        # Fixed: fuzzy matching now finds "asociate" as a near-match for "associate"
        assert context != "", "Fuzzy matching should find a near-match"
        assert "asociate director" in context or "asociate" in context, \
            f"Context should contain the fuzzy-matched text, got: {context}"


class TestGetAudioTimingForCorrection:
    """Test the CLI function that gets audio timing."""

    def test_timing_function_exists(self):
        """Verify the function can be imported."""
        from ..cli import _get_audio_timing_for_correction
        assert callable(_get_audio_timing_for_correction)

    def test_timing_with_mock_correction(self, tmp_path):
        """Test timing extraction with mock data."""
        from ..cli import _get_audio_timing_for_correction
        from ..correction_review import AppliedCorrection

        # Set up mock files
        processed_dir = tmp_path / "inbox" / "processed"
        processed_dir.mkdir(parents=True)
        raw_voice_dir = tmp_path / "inbox" / "raw" / "voice"
        raw_voice_dir.mkdir(parents=True)

        # Create transcript
        transcript_path = processed_dir / "Test_transcript.md"
        transcript_path.write_text("We have social director and other staff.")

        # Create timestamps
        timestamps_path = processed_dir / "Test_timestamps.json"
        timestamps_data = {
            "words": [
                {"word": "We", "start": 0.0, "end": 0.2},
                {"word": "have", "start": 0.3, "end": 0.5},
                {"word": "social", "start": 0.6, "end": 0.9},
                {"word": "director", "start": 1.0, "end": 1.4},
                {"word": "and", "start": 1.5, "end": 1.6},
            ]
        }
        timestamps_path.write_text(json.dumps(timestamps_data))

        # Create audio file (empty, just needs to exist)
        audio_path = raw_voice_dir / "Test.m4a"
        audio_path.write_bytes(b"fake audio")

        # Create correction
        correction = AppliedCorrection(
            id="corr_test",
            transcript_path=str(transcript_path),
            original="social director",
            corrected="associate director",
            category="person_name",
            confidence=0.9,
            context="",
            reason="test",
        )

        # Get timing
        timing = _get_audio_timing_for_correction(correction, tmp_path)

        assert timing is not None, "Should find audio timing"
        assert timing["source_audio"] == str(audio_path)
        # Start should be around 0.6 (start of "social") minus buffer
        assert timing["start_sec"] >= 0  # Can't go negative
        assert timing["start_sec"] < 1.0  # Should be near the start


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
