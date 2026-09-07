# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Correction Review System — non-blocking learning for transcription.

Design principles:
1. NEVER BLOCK the pipeline - corrections auto-apply based on confidence
2. Learning happens from DOWNSTREAM ERROR REPORTS, not upfront review
3. Anyone who sees an error can flag it → system learns
4. Optional proactive review for those who want it (not required)

Flow:
1. LLM proposes corrections → auto-applied if confidence >= threshold
2. Signals flow downstream to initiatives, endpoints, people
3. When someone sees an error: "flag error" → system learns NOT to make it
4. When someone confirms a correction was good: "confirm" → reinforces learning
5. Periodic digest shows correction stats (not a blocking queue)

Error reporting entry points:
- Signal view: "This looks wrong" button
- PRD owner sees misrouted signal: flags the bad correction
- Briefing shows wrong name: one-click "that's not right"

State files:
- correction_training.jsonl: Confirmed-good corrections (positive examples)
- correction_errors.jsonl: Flagged errors (negative examples)
- correction_applied.jsonl: All applied corrections (for audit trail)
- correction_stats.json: Accuracy metrics by category

Usage:
    axi signal corrections --stats            # Show accuracy stats
    axi signal corrections --flag <id>        # Flag an error (from signal view)
    axi signal corrections --confirm <id>     # Confirm a correction was good
    axi signal corrections --digest           # Weekly digest of correction quality
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.state import atomic_write, locked_append_jsonl

_RUNTIME_DIR = _REPO_ROOT / "runtime"
CORRECTIONS_DIR = _RUNTIME_DIR / "inbox" / "corrections"

# State files
APPLIED_FILE = CORRECTIONS_DIR / "correction_applied.jsonl"  # Audit trail
TRAINING_FILE = CORRECTIONS_DIR / "correction_training.jsonl"  # Confirmed good
ERRORS_FILE = CORRECTIONS_DIR / "correction_errors.jsonl"  # Flagged bad
STATS_FILE = CORRECTIONS_DIR / "correction_stats.json"  # Metrics

# Auto-apply threshold - corrections above this just apply (no blocking)
DEFAULT_AUTO_APPLY_THRESHOLD = 0.7


@dataclass
class AppliedCorrection:
    """Record of a correction that was auto-applied (audit trail).

    This is NOT a blocking queue item. Corrections apply automatically,
    and this record allows downstream error flagging.
    """

    id: str
    transcript_path: str
    original: str
    corrected: str
    category: str
    confidence: float
    context: str
    reason: str

    # Downstream linkage (for error flagging)
    signal_ids: list[str] = field(default_factory=list)
    published_endpoints: list[str] = field(default_factory=list)

    # Feedback state (filled in later, if ever)
    feedback: str = ""  # "confirmed", "flagged", or "" (no feedback yet)
    feedback_by: str = ""
    feedback_at: str = ""
    feedback_note: str = ""
    correct_value: str = ""  # If flagged, what it should have been

    applied_at: str = ""

    def __post_init__(self):
        if not self.applied_at:
            self.applied_at = datetime.now(UTC).isoformat()
        if not self.id:
            self.id = f"corr_{secrets.token_hex(6)}"

    def to_jsonl(self) -> str:
        return json.dumps({
            "id": self.id,
            "transcript_path": self.transcript_path,
            "original": self.original,
            "corrected": self.corrected,
            "category": self.category,
            "confidence": self.confidence,
            "context": self.context,
            "reason": self.reason,
            "signal_ids": self.signal_ids,
            "published_endpoints": self.published_endpoints,
            "feedback": self.feedback,
            "feedback_by": self.feedback_by,
            "feedback_at": self.feedback_at,
            "feedback_note": self.feedback_note,
            "correct_value": self.correct_value,
            "applied_at": self.applied_at,
        })

    @classmethod
    def from_jsonl(cls, line: str) -> AppliedCorrection:
        data = json.loads(line)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class TrainingExample:
    """An approved correction for the training set."""

    original: str
    corrected: str
    category: str
    context: str
    reason: str

    # Provenance
    source_transcript: str
    approved_by: str
    approved_at: str

    # Quality signal
    was_edited: bool = False  # Human improved the LLM's suggestion

    def to_jsonl(self) -> str:
        return json.dumps({
            "original": self.original,
            "corrected": self.corrected,
            "category": self.category,
            "context": self.context,
            "reason": self.reason,
            "source_transcript": self.source_transcript,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "was_edited": self.was_edited,
        })

    @classmethod
    def from_jsonl(cls, line: str) -> TrainingExample:
        data = json.loads(line)
        return cls(**data)


@dataclass
class ResynthesisJob:
    """A re-synthesis job triggered by correction."""

    id: str
    signal_id: str
    transcript_path: str
    correction_id: str
    published_endpoints: list[str] = field(default_factory=list)

    status: str = "pending"  # pending, in_progress, completed, failed
    created_at: str = ""
    completed_at: str = ""
    error: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(UTC).isoformat()
        if not self.id:
            self.id = f"resyn_{secrets.token_hex(6)}"


class CorrectionReviewSystem:
    """Non-blocking correction system with downstream error reporting.

    Key design: Corrections auto-apply → downstream catches errors → system learns

    Flow:
    1. record_applied() - Log correction as applied (audit trail)
    2. flag_error() - When someone notices mistake (e.g., JEDI owner)
    3. confirm() - When someone verifies correction was right
    4. get_accuracy_stats() - Track what's working, what's not
    """

    def __init__(self, corrections_dir: Path | None = None):
        self.corrections_dir = corrections_dir or CORRECTIONS_DIR
        self.corrections_dir.mkdir(parents=True, exist_ok=True)

        # Audit trail of all applied corrections
        self.applied_file = self.corrections_dir / "correction_applied.jsonl"
        # Confirmed-good corrections (positive training)
        self.training_file = self.corrections_dir / "correction_training.jsonl"
        # Flagged errors (negative training)
        self.errors_file = self.corrections_dir / "correction_errors.jsonl"
        # Re-synthesis jobs
        self.resynthesis_queue = self.corrections_dir / "resynthesis_queue.json"
        # Accuracy stats cache
        self.stats_file = self.corrections_dir / "correction_stats.json"

    # ---------------------------------------------------------------------------
    # Non-blocking: Record applied corrections (audit trail)
    # ---------------------------------------------------------------------------

    def record_applied(
        self,
        correction_id: str,
        transcript_path: str,
        original: str,
        corrected: str,
        category: str,
        confidence: float,
        context: str,
        reason: str,
        signal_ids: list[str] | None = None,
        published_endpoints: list[str] | None = None,
    ) -> AppliedCorrection:
        """Record a correction that was auto-applied. NON-BLOCKING.

        Idempotent: skips if correction_id already exists (for safe reprocessing).
        Also skips if the pattern (original→corrected) has already been reviewed,
        to avoid cluttering the review queue with patterns already addressed.
        """
        # Check if already recorded (deterministic IDs make reprocessing idempotent)
        existing = self.get_applied(correction_id)
        if existing:
            return existing  # Already recorded, skip duplicate

        # Check if this pattern has already been reviewed (to avoid redundant entries)
        reviewed = self._get_reviewed_pattern(original, corrected)
        if reviewed:
            # Pattern already reviewed - create a pre-reviewed entry
            # so it doesn't clog the queue but still gets recorded
            correction = AppliedCorrection(
                id=correction_id,
                transcript_path=transcript_path,
                original=original,
                corrected=corrected,
                category=category,
                confidence=confidence,
                context=context,
                reason=reason,
                signal_ids=signal_ids or [],
                published_endpoints=published_endpoints or [],
                feedback=reviewed.feedback,
                feedback_by=f"{reviewed.feedback_by}_auto",
                feedback_at=datetime.now(UTC).isoformat(),
                feedback_note="Auto-inherited from reviewed sibling pattern",
                correct_value=reviewed.correct_value,
            )
            locked_append_jsonl(self.applied_file, json.loads(correction.to_jsonl()))
            return correction

        correction = AppliedCorrection(
            id=correction_id,
            transcript_path=transcript_path,
            original=original,
            corrected=corrected,
            category=category,
            confidence=confidence,
            context=context,
            reason=reason,
            signal_ids=signal_ids or [],
            published_endpoints=published_endpoints or [],
        )

        locked_append_jsonl(self.applied_file, json.loads(correction.to_jsonl()))
        return correction

    def get_applied(self, correction_id: str) -> AppliedCorrection | None:
        """Look up an applied correction by ID."""
        if not self.applied_file.exists():
            return None

        with open(self.applied_file) as f:
            for line in f:
                if not line.strip():
                    continue
                data = json.loads(line)
                if data.get("id") == correction_id:
                    return AppliedCorrection.from_jsonl(line)
        return None

    def get_all_applied(self, limit: int = 1000) -> list[AppliedCorrection]:
        """Get all applied corrections (recent first)."""
        if not self.applied_file.exists():
            return []

        corrections = []
        with open(self.applied_file) as f:
            for line in f:
                if not line.strip():
                    continue
                corrections.append(AppliedCorrection.from_jsonl(line))

        # Most recent first
        corrections.reverse()
        return corrections[:limit]

    def get_unfeedback_corrections(self, limit: int = 100) -> list[AppliedCorrection]:
        """Get applied corrections that haven't received any feedback yet."""
        corrections = self.get_all_applied(limit=10000)
        return [c for c in corrections if not c.feedback][:limit]

    def get_matching_pattern_corrections(
        self,
        original: str,
        corrected: str,
        exclude_id: str | None = None,
    ) -> list[AppliedCorrection]:
        """Find all unfeedback corrections with the same original→corrected pattern.

        This enables batch confirmation: confirm one instance of a pattern,
        and auto-confirm all other instances of the same correction.

        Args:
            original: The original text being corrected
            corrected: The corrected text
            exclude_id: ID to exclude (usually the one already confirmed)

        Returns:
            List of matching corrections without feedback
        """
        unfeedback = self.get_unfeedback_corrections(limit=10000)
        matches = []
        for c in unfeedback:
            if c.original == original and c.corrected == corrected:
                if exclude_id and c.id == exclude_id:
                    continue
                matches.append(c)
        return matches

    def _get_reviewed_pattern(
        self,
        original: str,
        corrected: str,
    ) -> AppliedCorrection | None:
        """Find a reviewed correction with the same original→corrected pattern.

        Used during record_applied() to auto-inherit feedback from sibling
        patterns that have already been reviewed, preventing duplicate queue entries.

        Args:
            original: The original text being corrected
            corrected: The corrected text

        Returns:
            A matching reviewed correction if found, None otherwise
        """
        all_applied = self.get_all_applied(limit=10000)
        for c in all_applied:
            if c.original == original and c.corrected == corrected and c.feedback:
                return c
        return None

    def batch_confirm_pattern(
        self,
        original: str,
        corrected: str,
        confirmed_by: str,
        exclude_id: str | None = None,
    ) -> list[AppliedCorrection]:
        """Confirm all corrections matching a pattern (auto-confirm siblings).

        When a user confirms one instance of a correction pattern like
        'warm of the rock' → 'majority of the ROC', this finds and confirms
        all other instances of that same pattern.

        Args:
            original: The original text being corrected
            corrected: The corrected text
            confirmed_by: Who confirmed
            exclude_id: ID to exclude (usually already confirmed manually)

        Returns:
            List of corrections that were auto-confirmed
        """
        matches = self.get_matching_pattern_corrections(original, corrected, exclude_id)
        confirmed = []
        for c in matches:
            result = self.confirm_correct(
                c.id,
                confirmed_by=confirmed_by,
                note="Auto-confirmed: same pattern as manual confirmation",
            )
            if result:
                confirmed.append(result)
        return confirmed

    # ---------------------------------------------------------------------------
    # Downstream error flagging (the key non-blocking mechanism)
    # ---------------------------------------------------------------------------

    def flag_error(
        self,
        correction_id: str,
        flagged_by: str,
        actual_correct: str,
        reason: str = "",
    ) -> AppliedCorrection:
        """Flag a correction as wrong (downstream user reports error).

        This is how errors like "jet design" → "JEDI" get caught:
        The JEDI initiative owner sees the briefing, says "this isn't about JEDI",
        and flags it. The system learns not to make that correction again.

        Args:
            correction_id: ID of the applied correction
            flagged_by: Who flagged it (email or name)
            actual_correct: What it should have been (e.g., "legit")
            reason: Why this was wrong

        Returns:
            Updated correction with feedback
        """
        correction = self._update_feedback(
            correction_id,
            feedback_status="flagged_error",
            feedback_by=flagged_by,
            actual_correct=actual_correct,
            feedback_note=reason,
        )

        if correction:
            # Add to errors file (negative training)
            self._add_to_errors(correction, actual_correct, flagged_by, reason)

            # Queue re-synthesis to fix the published content
            self._queue_resynthesis_for_error(correction)

        return correction  # type: ignore

    def confirm_correct(
        self,
        correction_id: str,
        confirmed_by: str,
        note: str = "",
    ) -> AppliedCorrection:
        """Confirm a correction was right (positive feedback).

        This builds confidence — confirmed corrections become positive training examples.
        """
        correction = self._update_feedback(
            correction_id,
            feedback_status="confirmed",
            feedback_by=confirmed_by,
            feedback_note=note,
        )

        if correction:
            # Add to training file (positive training)
            self._add_to_training(correction, confirmed_by)

        return correction  # type: ignore

    def mark_unknown(
        self,
        correction_id: str,
        marked_by: str,
        note: str = "",
    ) -> AppliedCorrection | None:
        """Mark a correction as unknown/unintelligible.

        Use when the audio is unclear and the correct interpretation cannot
        be determined. This removes the correction from the review queue
        without affecting training data (neither positive nor negative).
        """
        return self._update_feedback(
            correction_id,
            feedback_status="unknown",
            feedback_by=marked_by,
            feedback_note=note or "Unintelligible audio",
        )

    def confirm_with_edit(
        self,
        correction_id: str,
        new_corrected_value: str,
        confirmed_by: str,
        note: str = "",
    ) -> AppliedCorrection | None:
        """Confirm with a user-provided correction value.

        Used when the LLM's suggestion was close but not quite right.
        This updates the corrected value AND marks as confirmed.

        Learning: This teaches the consumer the reviewer's preferred corrections.
        """
        if not self.applied_file.exists():
            return None

        corrections = []
        updated = None

        with open(self.applied_file) as f:
            for line in f:
                if not line.strip():
                    continue
                corr = AppliedCorrection.from_jsonl(line)
                if corr.id == correction_id:
                    # Store original LLM suggestion for learning
                    original_suggestion = corr.corrected
                    # Update to user's value
                    corr.corrected = new_corrected_value
                    corr.feedback = "confirmed"
                    corr.feedback_by = confirmed_by
                    corr.feedback_at = datetime.now(UTC).isoformat()
                    corr.feedback_note = note or f"Edited from '{original_suggestion}'"
                    corr.correct_value = new_corrected_value  # For training
                    updated = corr
                corrections.append(corr)

        if updated:
            # Rewrite file
            with open(self.applied_file, "w") as f:
                for corr in corrections:
                    f.write(corr.to_jsonl() + "\n")

            # Add to training with the user's correction
            self._add_to_training(updated, confirmed_by)

        return updated

    def _update_feedback(
        self,
        correction_id: str,
        feedback_status: str,
        feedback_by: str,
        actual_correct: str | None = None,
        feedback_note: str = "",
    ) -> AppliedCorrection | None:
        """Update feedback on an applied correction (rewrite entire file)."""
        if not self.applied_file.exists():
            return None

        corrections = []
        updated = None

        with open(self.applied_file) as f:
            for line in f:
                if not line.strip():
                    continue
                corr = AppliedCorrection.from_jsonl(line)
                if corr.id == correction_id:
                    corr.feedback = feedback_status
                    corr.feedback_by = feedback_by
                    corr.feedback_at = datetime.now(UTC).isoformat()
                    corr.feedback_note = feedback_note
                    if actual_correct:
                        corr.correct_value = actual_correct
                    updated = corr
                corrections.append(corr)

        if updated:
            # Rewrite file
            with open(self.applied_file, "w") as f:
                for corr in corrections:
                    f.write(corr.to_jsonl() + "\n")

        return updated

    # ---------------------------------------------------------------------------
    # Training data management (learning from feedback)
    # ---------------------------------------------------------------------------

    def _add_to_training(self, correction: AppliedCorrection, confirmed_by: str) -> None:
        """Add confirmed-correct correction to training set (positive example)."""
        example = TrainingExample(
            original=correction.original,
            corrected=correction.corrected,
            category=correction.category,
            context=correction.context,
            reason=correction.reason,
            source_transcript=correction.transcript_path,
            approved_by=confirmed_by,
            approved_at=datetime.now(UTC).isoformat(),
            was_edited=False,  # It was auto-applied correctly
        )

        locked_append_jsonl(self.training_file, json.loads(example.to_jsonl()))

    def _add_to_errors(
        self,
        correction: AppliedCorrection,
        actual_correct: str,
        flagged_by: str,
        reason: str,
    ) -> None:
        """Add flagged error to negative examples (things NOT to do)."""
        data = {
            "original": correction.original,
            "suggested": correction.corrected,  # What LLM incorrectly suggested
            "actual_correct": actual_correct,   # What it should have been
            "flagged_by": flagged_by,
            "flagged_at": datetime.now(UTC).isoformat(),
            "reason": reason,
            "context": correction.context,
            "category": correction.category,
            "confidence": correction.confidence,  # Track confidence of bad corrections
        }

        locked_append_jsonl(self.errors_file, data)

    def _queue_resynthesis_for_error(self, correction: AppliedCorrection) -> ResynthesisJob | None:
        """Queue re-synthesis to fix published content after error flagged."""
        if not correction.signal_ids or not correction.published_endpoints:
            return None

        jobs = self._load_resynthesis_queue()

        for signal_id in correction.signal_ids:
            job = ResynthesisJob(
                id=f"resyn_{secrets.token_hex(6)}",
                signal_id=signal_id,
                transcript_path=correction.transcript_path,
                correction_id=correction.id,
                published_endpoints=correction.published_endpoints,
            )
            jobs.append(job)

        self._save_resynthesis_queue(jobs)
        return jobs[-1] if jobs else None

    def get_training_examples(
        self,
        category: str | None = None,
        limit: int = 50,
    ) -> list[TrainingExample]:
        """Get training examples for few-shot prompting.

        Args:
            category: Filter by category (person_name, technical_term, etc.)
            limit: Maximum examples to return

        Returns:
            List of approved training examples
        """
        if not self.training_file.exists():
            return []

        examples = []
        with open(self.training_file) as f:
            for line in f:
                if not line.strip():
                    continue
                example = TrainingExample.from_jsonl(line)
                if category and example.category != category:
                    continue
                examples.append(example)

        # Prioritize human-edited examples (higher quality signal)
        examples.sort(key=lambda e: (e.was_edited, e.approved_at), reverse=True)

        return examples[:limit]

    def get_training_stats(self) -> dict:
        """Get statistics about training/error data."""
        examples = self.get_training_examples(limit=10000)

        by_category = {}
        for ex in examples:
            by_category[ex.category] = by_category.get(ex.category, 0) + 1

        # Count errors
        error_count = 0
        errors_by_category = {}
        if self.errors_file.exists():
            with open(self.errors_file) as f:
                for line in f:
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    error_count += 1
                    cat = data.get("category", "unknown")
                    errors_by_category[cat] = errors_by_category.get(cat, 0) + 1

        # Get applied counts
        applied_count = 0
        feedback_pending = 0
        if self.applied_file.exists():
            with open(self.applied_file) as f:
                for line in f:
                    if not line.strip():
                        continue
                    applied_count += 1
                    data = json.loads(line)
                    # No feedback yet = pending review
                    if not data.get("feedback"):
                        feedback_pending += 1

        return {
            "total_applied": applied_count,
            "total_confirmed": len(examples),
            "total_errors": error_count,
            "feedback_pending": feedback_pending,
            "by_category": by_category,
            "errors_by_category": errors_by_category,
            "accuracy": len(examples) / max(len(examples) + error_count, 1),
        }

    def _load_resynthesis_queue(self) -> list[ResynthesisJob]:
        """Load re-synthesis queue."""
        if not self.resynthesis_queue.exists():
            return []
        data = json.loads(self.resynthesis_queue.read_text())
        return [ResynthesisJob(**j) for j in data.get("jobs", [])]

    def _save_resynthesis_queue(self, jobs: list[ResynthesisJob]) -> None:
        """Save re-synthesis queue."""
        data = {
            "updated_at": datetime.now(UTC).isoformat(),
            "jobs": [{"id": j.id, "signal_id": j.signal_id, "transcript_path": j.transcript_path,
                     "correction_id": j.correction_id, "published_endpoints": j.published_endpoints,
                     "status": j.status, "created_at": j.created_at, "completed_at": j.completed_at,
                     "error": j.error} for j in jobs],
        }
        atomic_write(self.resynthesis_queue, data)

    def get_pending_resynthesis(self) -> list[ResynthesisJob]:
        """Get pending re-synthesis jobs."""
        return [j for j in self._load_resynthesis_queue() if j.status == "pending"]

    def complete_resynthesis(self, job_id: str, success: bool = True, error: str = "") -> None:
        """Mark a re-synthesis job as complete."""
        jobs = self._load_resynthesis_queue()

        for job in jobs:
            if job.id == job_id:
                job.status = "completed" if success else "failed"
                job.completed_at = datetime.now(UTC).isoformat()
                job.error = error
                break

        self._save_resynthesis_queue(jobs)

    # ---------------------------------------------------------------------------
    # Few-shot prompt generation
    # ---------------------------------------------------------------------------

    def get_fewshot_examples_for_prompt(
        self,
        max_examples: int = 10,
    ) -> str:
        """Generate few-shot examples for correction prompt.

        This is the reinforcement learning payoff — approved human corrections
        become examples that improve future LLM corrections.
        """
        examples = self.get_training_examples(limit=max_examples)

        if not examples:
            # Fallback to static examples
            return """
Examples of corrections:
- "new tronics" → "neutronics" (technical_term, confidence: 0.95)
  Reason: Technical term for neutron physics analysis
- "any U.P." → "RUP" (acronym, confidence: 0.92)
  Reason: Research University Program
"""

        lines = ["Examples of approved corrections (from human reviewers):"]

        for ex in examples:
            quality = "[edited]" if ex.was_edited else ""
            lines.append(
                f'- "{ex.original}" → "{ex.corrected}" ({ex.category}) {quality}\n'
                f"  Reason: {ex.reason}"
            )

        return "\n".join(lines)

    def get_negative_examples_for_prompt(self, max_examples: int = 5) -> str:
        """Generate negative examples (corrections to avoid)."""
        if not self.errors_file.exists():
            return ""

        lines = ["AVOID these incorrect corrections (rejected by reviewers):"]

        with open(self.errors_file) as f:
            for i, line in enumerate(f):
                if i >= max_examples:
                    break
                data = json.loads(line)
                lines.append(
                    f'- "{data["original"]}" → "{data["suggested"]}" was WRONG\n'
                    f"  Reason: {data.get('reason', 'Rejected by reviewer')}"
                )

        return "\n".join(lines) if len(lines) > 1 else ""


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _get_centered_context(context: str, original: str, width: int = 80) -> str:
    """Get a display snippet of context centered on the original term.

    Instead of showing the first N characters (which may not include the term),
    this finds the term in the context and shows text centered around it.

    Args:
        context: Full context string
        original: The original term to center on
        width: Total width of the display snippet

    Returns:
        A snippet like "...text before [ORIGINAL] text after..."
    """
    if not context:
        return "(no context)"

    # Find the original term (case-insensitive)
    lower_context = context.lower()
    lower_original = original.lower()
    idx = lower_context.find(lower_original)

    if idx == -1:
        # Term not found - just show what we have, truncated
        if len(context) <= width:
            return context
        return context[:width] + "..."

    # Calculate window centered on the term
    term_len = len(original)
    half_width = (width - term_len) // 2

    start = max(0, idx - half_width)
    end = min(len(context), idx + term_len + half_width)

    snippet = context[start:end]

    # Add ellipsis indicators
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(context) else ""

    return f"{prefix}{snippet}{suffix}"


def print_unfeedback_corrections() -> None:
    """Print applied corrections awaiting feedback."""
    system = CorrectionReviewSystem()
    corrections = system.get_unfeedback_corrections(limit=20)

    if not corrections:
        print("No corrections awaiting feedback.")
        return

    print(f"\n{len(corrections)} correction(s) awaiting feedback:\n")

    for corr in corrections:
        conf_pct = int(corr.confidence * 100)
        status_icon = "⏳" if not corr.feedback else "✓"
        print(f"  {status_icon} [{corr.id}] {corr.original!r} → {corr.corrected!r}")
        print(f"    Category: {corr.category} | Confidence: {conf_pct}%")
        # Show context centered on the original term
        context_display = _get_centered_context(corr.context, corr.original, width=80)
        print(f"    Context: {context_display}")
        if corr.signal_ids:
            print(f"    Signals: {', '.join(corr.signal_ids[:3])}")
        print()


def print_recent_errors() -> None:
    """Print recently flagged errors."""
    system = CorrectionReviewSystem()

    if not system.errors_file.exists():
        print("No errors flagged yet.")
        return

    errors = []
    with open(system.errors_file) as f:
        for line in f:
            if line.strip():
                errors.append(json.loads(line))

    if not errors:
        print("No errors flagged yet.")
        return

    # Show last 10
    errors = errors[-10:]
    errors.reverse()

    print("\n=== Recent Flagged Errors ===\n")

    for err in errors:
        print(f"  ✗ {err['original']!r} → {err['suggested']!r} was WRONG")
        print(f"    Actual: {err.get('actual_correct', '?')!r}")
        print(f"    Flagged by: {err.get('flagged_by', 'unknown')}")
        if err.get('reason'):
            print(f"    Reason: {err['reason']}")
        print()


def print_training_stats() -> None:
    """Print training data statistics."""
    system = CorrectionReviewSystem()
    stats = system.get_training_stats()

    print("\n=== Correction Learning Stats ===")
    print(f"  Total applied: {stats['total_applied']}")
    print(f"  Confirmed correct: {stats['total_confirmed']}")
    print(f"  Flagged errors: {stats['total_errors']}")
    print(f"  Awaiting feedback: {stats['feedback_pending']}")
    print(f"  Accuracy: {stats['accuracy']:.1%}")

    if stats['by_category']:
        print("\n  Confirmed by category:")
        for cat, count in sorted(stats["by_category"].items(), key=lambda x: -x[1]):
            print(f"    {cat}: {count}")

    if stats['errors_by_category']:
        print("\n  Errors by category:")
        for cat, count in sorted(stats["errors_by_category"].items(), key=lambda x: -x[1]):
            print(f"    {cat}: {count}")
    print()


# Backwards compatibility alias
print_pending_corrections = print_unfeedback_corrections
