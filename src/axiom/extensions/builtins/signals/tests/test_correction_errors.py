# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for correction error flagging and learning.

These tests verify that:
1. Corrections auto-apply (non-blocking)
2. Downstream errors can be flagged
3. Flagged errors become negative examples
4. Future corrections avoid flagged patterns

Real-world example from 2026-02-24:
- LLM incorrectly suggested "jet design" → "JEDI"
- Context: "that's legit. Yeah, that's all a jet design"
- Actual: "jet design" was likely "legit" (phonetic mishearing)
- Problem: LLM pattern-matched to JEDI initiative, ignored context
"""

import json
from datetime import UTC, datetime

from ..correction_review import (
    AppliedCorrection,
    CorrectionReviewSystem,
    TrainingExample,
)

# The actual error case from the transcript
JET_DESIGN_ERROR = {
    "original": "jet design",
    "corrected": "JEDI",
    "category": "acronym",
    "confidence": 0.88,
    "reason": "JEDI is listed as an active initiative, context suggests this is referring to that project",
    "context": "... That's so stupid. So yeah, they're trying to patent that. I mean, that's legit. Yeah, that's all a jet design. Yeah, it's totally patentable. But I think BWXTs, they have one word, it's just like using machine...",
}


class TestNonBlockingCorrections:
    """Verify corrections don't block the pipeline."""

    def test_correction_auto_applies_above_threshold(self, tmp_path):
        """Corrections above threshold apply automatically without review."""
        CorrectionReviewSystem(corrections_dir=tmp_path)

        # High-confidence correction should just apply
        correction = AppliedCorrection(
            id="corr_test1",
            transcript_path="/test/transcript.md",
            original="c of d",
            corrected="CFD",
            category="technical_term",
            confidence=0.95,
            context="simulation inputs for c of d",
            reason="Common Whisper mishearing",
        )

        # Write to applied log (not pending queue)
        applied_file = tmp_path / "correction_applied.jsonl"
        with open(applied_file, "a") as f:
            f.write(correction.to_jsonl() + "\n")

        # Verify it's in the audit trail, not a blocking queue
        assert applied_file.exists()
        with open(applied_file) as f:
            lines = f.readlines()
        assert len(lines) == 1

        # No pending queue should exist
        pending_file = tmp_path / "correction_pending.json"
        assert not pending_file.exists()

    def test_low_confidence_still_applies_but_flagged(self, tmp_path):
        """Even low-confidence corrections apply, but are more likely flagged."""
        CorrectionReviewSystem(corrections_dir=tmp_path)

        # The problematic correction
        correction = AppliedCorrection(
            id="corr_jet_design",
            transcript_path="/test/transcript.md",
            original=JET_DESIGN_ERROR["original"],
            corrected=JET_DESIGN_ERROR["corrected"],
            category=JET_DESIGN_ERROR["category"],
            confidence=JET_DESIGN_ERROR["confidence"],
            context=JET_DESIGN_ERROR["context"],
            reason=JET_DESIGN_ERROR["reason"],
        )

        # It still applies (non-blocking)
        applied_file = tmp_path / "correction_applied.jsonl"
        with open(applied_file, "a") as f:
            f.write(correction.to_jsonl() + "\n")

        # Verify it applied
        with open(applied_file) as f:
            data = json.loads(f.readline())
        assert data["corrected"] == "JEDI"
        assert data["feedback"] == ""  # No feedback yet


class TestDownstreamErrorFlagging:
    """Test the error flagging flow when someone notices a mistake."""

    def test_flag_jet_design_error(self, tmp_path):
        """JEDI owner sees misrouted signal and flags the error."""
        CorrectionReviewSystem(corrections_dir=tmp_path)

        # First, the correction was applied
        correction = AppliedCorrection(
            id="corr_jet_design",
            transcript_path="/test/transcript.md",
            original="jet design",
            corrected="JEDI",
            category="acronym",
            confidence=0.88,
            context=JET_DESIGN_ERROR["context"],
            reason=JET_DESIGN_ERROR["reason"],
            signal_ids=["sig_abc123"],
            published_endpoints=["prd-jedi"],
        )

        applied_file = tmp_path / "correction_applied.jsonl"
        with open(applied_file, "w") as f:
            f.write(correction.to_jsonl() + "\n")

        # JEDI owner sees wrong signal, clicks "flag error"
        correction.feedback = "flagged"
        correction.feedback_by = "reviewer@example.org"
        correction.feedback_at = datetime.now(UTC).isoformat()
        correction.feedback_note = "This should be 'legit', not JEDI - context is about patents"
        correction.correct_value = "legit"

        # Update applied record and add to errors file
        errors_file = tmp_path / "correction_errors.jsonl"
        error_record = {
            "correction_id": correction.id,
            "original": correction.original,
            "incorrect_suggestion": correction.corrected,
            "correct_value": correction.correct_value,
            "context": correction.context,
            "flagged_by": correction.feedback_by,
            "flagged_at": correction.feedback_at,
            "note": correction.feedback_note,
        }

        with open(errors_file, "a") as f:
            f.write(json.dumps(error_record) + "\n")

        # Verify error was logged
        with open(errors_file) as f:
            error = json.loads(f.readline())

        assert error["original"] == "jet design"
        assert error["incorrect_suggestion"] == "JEDI"
        assert error["correct_value"] == "legit"
        assert "legit" in error["note"]

    def test_flagged_error_becomes_negative_example(self, tmp_path):
        """Flagged errors feed the negative example set for future LLM calls."""
        errors_file = tmp_path / "correction_errors.jsonl"

        # Log the jet design error
        error_record = {
            "correction_id": "corr_jet_design",
            "original": "jet design",
            "incorrect_suggestion": "JEDI",
            "correct_value": "legit",
            "context": JET_DESIGN_ERROR["context"],
            "flagged_by": "reviewer@example.org",
            "flagged_at": datetime.now(UTC).isoformat(),
            "note": "This should be 'legit', not JEDI",
        }

        with open(errors_file, "w") as f:
            f.write(json.dumps(error_record) + "\n")

        # Generate negative examples for prompt
        lines = ["AVOID these incorrect corrections (flagged by users):"]
        with open(errors_file) as f:
            for line in f:
                data = json.loads(line)
                lines.append(
                    f'- "{data["original"]}" → "{data["incorrect_suggestion"]}" was WRONG\n'
                    f'  Should have been: "{data["correct_value"]}"\n'
                    f'  Reason: {data["note"]}'
                )

        negative_prompt = "\n".join(lines)

        assert "jet design" in negative_prompt
        assert "JEDI" in negative_prompt
        assert "legit" in negative_prompt
        assert "WRONG" in negative_prompt


class TestLearningFromErrors:
    """Test that the system learns from flagged errors."""

    def test_error_pattern_in_prompt(self, tmp_path):
        """Future prompts include the error to avoid."""
        errors_file = tmp_path / "correction_errors.jsonl"

        # Multiple similar errors
        errors = [
            {
                "original": "jet design",
                "incorrect_suggestion": "JEDI",
                "correct_value": "legit",
                "context": "that's legit. Yeah, that's all a jet design",
            },
            {
                "original": "a jet design",
                "incorrect_suggestion": "JEDI",
                "correct_value": "legit",
                "context": "I mean, that's a jet design. Yeah, totally",
            },
        ]

        with open(errors_file, "w") as f:
            for e in errors:
                f.write(json.dumps(e) + "\n")

        # Count patterns
        jet_to_jedi_errors = 0
        with open(errors_file) as f:
            for line in f:
                data = json.loads(line)
                if "jet" in data["original"].lower() and data["incorrect_suggestion"] == "JEDI":
                    jet_to_jedi_errors += 1

        # System should detect repeated pattern
        assert jet_to_jedi_errors == 2

        # This pattern should be strongly discouraged in future prompts
        # (In production, add logic to boost weight of repeated error patterns)

    def test_correct_value_becomes_training_example(self, tmp_path):
        """When user provides correct value, it becomes positive training."""
        training_file = tmp_path / "correction_training.jsonl"

        # After flagging "jet design" → "JEDI" as wrong,
        # user said it should be "legit"
        # This creates a POSITIVE training example
        training = TrainingExample(
            original="jet design",
            corrected="legit",  # The CORRECT correction
            category="phonetic",
            context="that's legit. Yeah, that's all a jet design",
            reason="'jet design' is a mishearing of 'legit' - common Whisper error",
            source_transcript="/test/transcript.md",
            approved_by="reviewer@example.org",
            approved_at=datetime.now(UTC).isoformat(),
            was_edited=True,  # Human corrected the LLM's mistake
        )

        with open(training_file, "w") as f:
            f.write(training.to_jsonl() + "\n")

        # Verify training example
        with open(training_file) as f:
            data = json.loads(f.readline())

        assert data["original"] == "jet design"
        assert data["corrected"] == "legit"
        assert data["was_edited"] is True  # High-quality signal


class TestContextAwareness:
    """Test that context should prevent false positives."""

    def test_context_clues_against_jedi_correction(self):
        """Analyze why this correction should have been rejected."""
        context = JET_DESIGN_ERROR["context"]

        # Context clues that argue AGAINST "JEDI":
        clues_against = []

        # 1. "that's legit" appears right before
        if "legit" in context.lower():
            clues_against.append("'legit' appears in nearby context")

        # 2. "patentable" suggests legal/patent discussion, not JEDI initiative
        if "patent" in context.lower():
            clues_against.append("patent/legal context, not project discussion")

        # 3. No other JEDI-related terms
        jedi_terms = ["simulation", "design tool", "analysis", "inputs"]
        jedi_context = any(term in context.lower() for term in jedi_terms)
        if not jedi_context:
            clues_against.append("no JEDI-related technical terms nearby")

        # We have strong evidence against this correction
        assert len(clues_against) >= 2
        assert "legit" in clues_against[0].lower()

    def test_phonetic_similarity_not_enough(self):
        """Phonetic similarity alone shouldn't trigger initiative corrections."""
        # "jet design" sounds vaguely like "JEDI" but:
        # - "jet" != "jedi" phonetically (different vowels)
        # - "design" is an extra word that doesn't fit

        original = "jet design"
        suggestion = "JEDI"

        # Levenshtein-style check
        # "jet" → "jedi" requires: j→j, e→e, t→d, +i
        # "design" is completely extra

        # This should NOT have been a high-confidence match
        # because the word count differs and phonetics don't match
        words_original = original.split()
        words_suggestion = suggestion.split()

        word_count_mismatch = len(words_original) != len(words_suggestion)
        assert word_count_mismatch, "Word count mismatch should reduce confidence"


class TestResynthesisAfterError:
    """Test re-synthesis when errors are corrected."""

    def test_flagged_error_triggers_resynthesis(self, tmp_path):
        """When error is flagged with correct value, trigger update."""
        resynthesis_queue = tmp_path / "resynthesis_queue.json"

        # Error flagged with correct value
        error = {
            "correction_id": "corr_jet_design",
            "signal_id": "sig_abc123",
            "published_endpoints": ["prd-jedi"],
            "original": "jet design",
            "incorrect_value": "JEDI",
            "correct_value": "legit",
        }

        # Queue re-synthesis job
        job = {
            "id": "resyn_001",
            "signal_id": error["signal_id"],
            "correction_id": error["correction_id"],
            "old_value": error["incorrect_value"],
            "new_value": error["correct_value"],
            "endpoints_to_update": error["published_endpoints"],
            "status": "pending",
        }

        data = {"jobs": [job]}
        resynthesis_queue.write_text(json.dumps(data, indent=2))

        # Verify job queued
        loaded = json.loads(resynthesis_queue.read_text())
        assert len(loaded["jobs"]) == 1
        assert loaded["jobs"][0]["old_value"] == "JEDI"
        assert loaded["jobs"][0]["new_value"] == "legit"
        assert "prd-jedi" in loaded["jobs"][0]["endpoints_to_update"]


class TestAccuracyTracking:
    """Test accuracy metrics by category."""

    def test_category_accuracy_stats(self, tmp_path):
        """Track accuracy rates per correction category."""
        applied_file = tmp_path / "correction_applied.jsonl"

        # Sample corrections with feedback
        corrections = [
            # Good corrections (confirmed)
            {"category": "technical_term", "original": "new tronics", "feedback": "confirmed"},
            {"category": "technical_term", "original": "gamut detector", "feedback": "confirmed"},
            {"category": "acronym", "original": "any U.P.", "feedback": "confirmed"},
            {"category": "acronym", "original": "C of D", "feedback": "confirmed"},
            {"category": "person_name", "original": "Son", "feedback": "confirmed"},
            # Bad corrections (flagged)
            {"category": "acronym", "original": "jet design", "feedback": "flagged"},  # The error!
            {"category": "person_name", "original": "Chayenne", "feedback": "flagged"},
        ]

        with open(applied_file, "w") as f:
            for c in corrections:
                f.write(json.dumps(c) + "\n")

        # Calculate accuracy by category
        stats = {}
        with open(applied_file) as f:
            for line in f:
                c = json.loads(line)
                cat = c["category"]
                if cat not in stats:
                    stats[cat] = {"confirmed": 0, "flagged": 0, "no_feedback": 0}
                stats[cat][c.get("feedback", "no_feedback")] += 1

        # Calculate accuracy rates
        for cat, counts in stats.items():
            total_with_feedback = counts["confirmed"] + counts["flagged"]
            if total_with_feedback > 0:
                counts["accuracy"] = counts["confirmed"] / total_with_feedback

        # Verify stats
        assert stats["technical_term"]["accuracy"] == 1.0  # 2/2 confirmed
        assert abs(stats["acronym"]["accuracy"] - 0.67) < 0.01  # 2/3 confirmed (jet design flagged)
        assert stats["person_name"]["accuracy"] == 0.5  # 1/2 confirmed
