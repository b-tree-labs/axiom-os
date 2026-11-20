# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Guided Correction Review — interactive review with audio clip support.

Features:
1. Interactive review mode the assistant can suggest during conversations
2. Priority/deferral tracking — "not now" queues for later
3. Audio clip extraction — stores relevant audio segment for review
4. Temporary storage with automatic cleanup after approval
5. Learning pipeline integration on confirmation

Usage:
    # During conversation, the assistant might say:
    # "I have 5 corrections from yesterday's voice memos awaiting review.
    #  Would you like to review them now, or should I remind you later?"

    axi signal corrections --guided          # Start guided review
    axi signal corrections --guided --limit 3  # Review up to 3
    axi signal corrections --defer           # Defer all pending to later
    axi signal corrections --cleanup         # Remove processed clips
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.state import LockedJsonFile

from .models import DATETIME_FORMAT_COMPACT

_RUNTIME_DIR = _REPO_ROOT / "runtime"
CLIPS_DIR = _RUNTIME_DIR / "inbox" / "corrections" / "audio_clips"
REVIEW_STATE_FILE = _RUNTIME_DIR / "inbox" / "corrections" / "review_state.json"


@dataclass
class AudioClip:
    """Reference to an extracted audio clip for correction review."""
    clip_id: str
    correction_id: str
    source_audio_path: str
    clip_path: str
    start_time_sec: float
    end_time_sec: float
    duration_sec: float
    created_at: str
    transcript_segment: str

    # Lifecycle
    status: str = "pending"  # pending, approved, rejected, expired
    processed_at: str = ""

    def to_dict(self) -> dict:
        return {
            "clip_id": self.clip_id,
            "correction_id": self.correction_id,
            "source_audio_path": self.source_audio_path,
            "clip_path": self.clip_path,
            "start_time_sec": self.start_time_sec,
            "end_time_sec": self.end_time_sec,
            "duration_sec": self.duration_sec,
            "created_at": self.created_at,
            "transcript_segment": self.transcript_segment,
            "status": self.status,
            "processed_at": self.processed_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> AudioClip:
        return cls(**data)


@dataclass
class ReviewState:
    """Tracks guided review state and deferral schedule."""
    last_review: str = ""
    last_prompted: str = ""
    deferred_until: str = ""  # ISO timestamp when to prompt again
    deferral_count: int = 0   # How many times user has deferred
    pending_clips: list[str] = field(default_factory=list)  # clip_ids

    # Review session tracking
    reviewed_today: int = 0
    approved_today: int = 0
    rejected_today: int = 0

    # User preferences (learned over time)
    preferred_batch_size: int = 5
    preferred_review_time: str = ""  # e.g., "09:00" if user tends to review in morning

    def to_dict(self) -> dict:
        return {
            "last_review": self.last_review,
            "last_prompted": self.last_prompted,
            "deferred_until": self.deferred_until,
            "deferral_count": self.deferral_count,
            "pending_clips": self.pending_clips,
            "reviewed_today": self.reviewed_today,
            "approved_today": self.approved_today,
            "rejected_today": self.rejected_today,
            "preferred_batch_size": self.preferred_batch_size,
            "preferred_review_time": self.preferred_review_time,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ReviewState:
        return cls(
            last_review=data.get("last_review", ""),
            last_prompted=data.get("last_prompted", ""),
            deferred_until=data.get("deferred_until", ""),
            deferral_count=data.get("deferral_count", 0),
            pending_clips=data.get("pending_clips", []),
            reviewed_today=data.get("reviewed_today", 0),
            approved_today=data.get("approved_today", 0),
            rejected_today=data.get("rejected_today", 0),
            preferred_batch_size=data.get("preferred_batch_size", 5),
            preferred_review_time=data.get("preferred_review_time", ""),
        )


class GuidedCorrectionReview:
    """Manages guided correction review with audio clip support."""

    # Cleanup policy
    CLIP_RETENTION_DAYS = 7  # Keep clips for 7 days after processing
    MAX_PENDING_CLIPS = 50   # Don't accumulate more than 50 pending clips

    def __init__(self):
        CLIPS_DIR.mkdir(parents=True, exist_ok=True)
        self._state = self._load_state()
        self._clips: dict[str, AudioClip] = self._load_clips()

    def _load_state(self) -> ReviewState:
        """Load review state from disk."""
        if REVIEW_STATE_FILE.exists():
            data: dict = {}
            try:
                with LockedJsonFile(REVIEW_STATE_FILE) as f:
                    data = f.read()
                return ReviewState.from_dict(data)
            except (json.JSONDecodeError, KeyError):
                pass
        return ReviewState()

    def _save_state(self) -> None:
        """Save review state to disk."""
        with LockedJsonFile(REVIEW_STATE_FILE, exclusive=True) as f:
            f.write(self._state.to_dict())

    def _load_clips(self) -> dict[str, AudioClip]:
        """Load all clip metadata."""
        clips = {}
        clips_index = CLIPS_DIR / "clips_index.json"
        if clips_index.exists():
            data: dict = {}
            try:
                with LockedJsonFile(clips_index) as f:
                    data = f.read()
                for clip_data in data.get("clips", []):
                    clip = AudioClip.from_dict(clip_data)
                    clips[clip.clip_id] = clip
            except (json.JSONDecodeError, KeyError):
                pass
        return clips

    def _save_clips(self) -> None:
        """Save clip metadata index."""
        clips_index = CLIPS_DIR / "clips_index.json"
        data = {
            "updated_at": datetime.now(UTC).isoformat(),
            "clips": [c.to_dict() for c in self._clips.values()],
        }
        with LockedJsonFile(clips_index, exclusive=True) as f:
            f.write(data)

    def get_clip_for_correction(self, correction_id: str) -> AudioClip | None:
        """Get existing clip for a correction ID (for deduplication)."""
        for clip in self._clips.values():
            if clip.correction_id == correction_id:
                return clip
        return None

    def should_prompt_review(self) -> tuple[bool, str]:
        """Check if Neut should suggest a correction review.

        Returns:
            Tuple of (should_prompt, reason_message)
        """
        now = datetime.now(UTC)

        # Check if deferred
        if self._state.deferred_until:
            defer_time = datetime.fromisoformat(self._state.deferred_until)
            if now < defer_time:
                return False, f"Deferred until {defer_time.strftime('%Y-%m-%d %H:%M')}"

        # Check pending count
        pending = self.get_pending_corrections()
        if not pending:
            return False, "No corrections pending review"

        # Check if recently prompted (don't nag)
        if self._state.last_prompted:
            last_prompt = datetime.fromisoformat(self._state.last_prompted)
            hours_since = (now - last_prompt).total_seconds() / 3600
            if hours_since < 4:  # Don't prompt more than every 4 hours
                return False, "Recently prompted"

        # Should prompt
        count = len(pending)
        self._state.last_prompted = now.isoformat()
        self._save_state()

        return True, f"I have {count} correction(s) from voice memos awaiting review."

    def get_pending_corrections(self) -> list[AudioClip]:
        """Get all corrections pending review."""
        return [c for c in self._clips.values() if c.status == "pending"]

    def defer_review(
        self,
        hours: int = 24,
        reason: str = "not_now",
    ) -> str:
        """Defer correction review to later.

        Args:
            hours: Hours to defer (default 24)
            reason: Why deferred (for learning user patterns)

        Returns:
            Confirmation message
        """
        defer_until = datetime.now(UTC) + timedelta(hours=hours)
        self._state.deferred_until = defer_until.isoformat()
        self._state.deferral_count += 1
        self._save_state()

        return f"Got it. I'll remind you about corrections in {hours} hours."

    def extract_audio_clip(
        self,
        correction_id: str,
        source_audio_path: str,
        start_time_sec: float,
        end_time_sec: float,
        transcript_segment: str,
        padding_sec: float = 1.0,
    ) -> AudioClip | None:
        """Extract audio clip for a correction.

        Uses ffmpeg to extract the relevant audio segment.

        Args:
            correction_id: ID of the correction this clip is for
            source_audio_path: Path to original audio file
            start_time_sec: Start time in seconds
            end_time_sec: End time in seconds
            transcript_segment: The transcript text for this segment
            padding_sec: Extra seconds to include before/after

        Returns:
            AudioClip object, or None if extraction failed
        """
        source_path = Path(source_audio_path)
        if not source_path.exists():
            return None

        # Delete any existing clip for this correction (allows regeneration with new timing)
        existing_clip = self.get_clip_for_correction(correction_id)
        if existing_clip:
            # Remove old clip file
            if existing_clip.clip_path:
                old_path = Path(existing_clip.clip_path)
                if old_path.exists():
                    try:
                        old_path.unlink()
                    except OSError:
                        pass
            # Remove from index
            if existing_clip.clip_id in self._clips:
                del self._clips[existing_clip.clip_id]
            if existing_clip.clip_id in self._state.pending_clips:
                self._state.pending_clips.remove(existing_clip.clip_id)

        # Apply padding
        start = max(0, start_time_sec - padding_sec)
        end = end_time_sec + padding_sec
        duration = end - start

        # Generate clip ID and path (deterministic based on correction_id)
        timestamp = datetime.now(UTC).strftime(DATETIME_FORMAT_COMPACT)
        clip_id = f"clip_{correction_id[:12]}"  # Deterministic ID
        clip_filename = f"clip_{correction_id}_{timestamp}.m4a"  # Filename can have timestamp for debugging
        clip_path = CLIPS_DIR / clip_filename

        # Check for ffmpeg
        if not shutil.which("ffmpeg"):
            # Fallback: just reference the source file with timestamps
            clip = AudioClip(
                clip_id=clip_id,
                correction_id=correction_id,
                source_audio_path=str(source_path),
                clip_path="",  # No extracted clip
                start_time_sec=start,
                end_time_sec=end,
                duration_sec=duration,
                created_at=datetime.now(UTC).isoformat(),
                transcript_segment=transcript_segment,
            )
            self._clips[clip_id] = clip
            self._state.pending_clips.append(clip_id)
            self._save_clips()
            self._save_state()
            return clip

        # Extract clip with ffmpeg
        try:
            cmd = [
                "ffmpeg",
                "-y",  # Overwrite
                "-i", str(source_path),
                "-ss", str(start),
                "-t", str(duration),
                "-c", "copy",  # Fast copy without re-encoding
                str(clip_path),
            ]
            subprocess.run(cmd, capture_output=True, check=True)

            clip = AudioClip(
                clip_id=clip_id,
                correction_id=correction_id,
                source_audio_path=str(source_path),
                clip_path=str(clip_path),
                start_time_sec=start,
                end_time_sec=end,
                duration_sec=duration,
                created_at=datetime.now(UTC).isoformat(),
                transcript_segment=transcript_segment,
            )
            self._clips[clip_id] = clip
            self._state.pending_clips.append(clip_id)
            self._save_clips()
            self._save_state()
            return clip

        except subprocess.CalledProcessError:
            return None

    def extract_clip_by_text(
        self,
        search_text: str,
        source_audio_path: str,
        duration_sec: float = 10.0,
        clip_id: str | None = None,
    ) -> AudioClip | None:
        """Extract audio clip centered on a text match.

        Finds the best match for search_text in the transcript timestamps,
        then extracts a clip centered on that position.

        Args:
            search_text: Text to search for in the transcript
            source_audio_path: Path to original audio file
            duration_sec: Total clip duration (centered on match)
            clip_id: Optional custom clip ID (auto-generated if not provided)

        Returns:
            AudioClip object, or None if no match found or extraction failed
        """
        import hashlib
        from difflib import SequenceMatcher

        source_path = Path(source_audio_path)
        if not source_path.exists():
            return None

        # Find timestamps JSON
        # Try common locations: same dir, processed dir, or inbox/processed
        timestamps_path = None
        stem = source_path.stem

        # Check same directory
        same_dir_path = source_path.parent / f"{stem}_timestamps.json"
        if same_dir_path.exists():
            timestamps_path = same_dir_path
        else:
            # Check processed directory (relative to inbox structure)
            processed_dir = source_path.parent.parent.parent / "processed"
            proc_path = processed_dir / f"{stem}_timestamps.json"
            if proc_path.exists():
                timestamps_path = proc_path
            else:
                # Check alongside transcript
                inbox_proc = _REPO_ROOT / "runtime" / "inbox" / "processed"
                inbox_path = inbox_proc / f"{stem}_timestamps.json"
                if inbox_path.exists():
                    timestamps_path = inbox_path

        if not timestamps_path or not timestamps_path.exists():
            return None

        # Load timestamps
        try:
            data = json.loads(timestamps_path.read_text(encoding="utf-8"))
            words = data.get("words", [])
        except (json.JSONDecodeError, OSError):
            return None

        if not words:
            return None

        # Build transcript text with word positions for fuzzy matching
        search_lower = search_text.lower().strip()
        search_words = search_lower.split()

        # Find best matching sequence
        best_score = 0.0
        best_start_idx = 0
        best_end_idx = 0

        # Sliding window search
        window_size = len(search_words)
        for i in range(len(words) - window_size + 1):
            window_text = " ".join(
                w.get("word", "").lower().strip(".,!?;:\"'")
                for w in words[i:i + window_size]
            )
            score = SequenceMatcher(None, search_lower, window_text).ratio()
            if score > best_score:
                best_score = score
                best_start_idx = i
                best_end_idx = i + window_size - 1

        # Also try partial matches for longer search strings
        if len(search_words) > 3:
            for window_mult in [0.5, 0.75, 1.25, 1.5]:
                adj_size = max(2, int(window_size * window_mult))
                for i in range(len(words) - adj_size + 1):
                    window_text = " ".join(
                        w.get("word", "").lower().strip(".,!?;:\"'")
                        for w in words[i:i + adj_size]
                    )
                    score = SequenceMatcher(None, search_lower, window_text).ratio()
                    if score > best_score:
                        best_score = score
                        best_start_idx = i
                        best_end_idx = i + adj_size - 1

        # Require reasonable match quality
        if best_score < 0.4:
            return None

        # Get timing - center the clip on the match
        match_start = words[best_start_idx].get("start", 0)
        match_end = words[best_end_idx].get("end", match_start + 2)
        match_center = (match_start + match_end) / 2

        # Calculate clip bounds centered on match
        half_duration = duration_sec / 2
        start_time = max(0, match_center - half_duration)
        end_time = match_center + half_duration

        # Generate clip ID if not provided
        if not clip_id:
            content_hash = hashlib.sha256(
                f"{source_audio_path}|{search_text}".encode()
            ).hexdigest()[:12]
            clip_id = f"text_{content_hash}"

        # Get matched text for the transcript_segment
        matched_text = " ".join(
            w.get("word", "") for w in words[best_start_idx:best_end_idx + 1]
        )

        return self.extract_audio_clip(
            correction_id=clip_id,
            source_audio_path=source_audio_path,
            start_time_sec=start_time,
            end_time_sec=end_time,
            transcript_segment=matched_text,
            padding_sec=0,  # Already accounted for in duration_sec
        )

    def approve_clip(self, clip_id: str) -> bool:
        """Mark a clip's correction as approved.

        This triggers learning pipeline integration.
        """
        clip = self._clips.get(clip_id)
        if not clip:
            return False

        clip.status = "approved"
        clip.processed_at = datetime.now(UTC).isoformat()

        # Update state
        if clip_id in self._state.pending_clips:
            self._state.pending_clips.remove(clip_id)
        self._state.approved_today += 1
        self._state.reviewed_today += 1

        self._save_clips()
        self._save_state()

        # Trigger learning and downstream propagation
        self._trigger_learning(
            clip.correction_id,
            approved=True,
            clip_id=clip_id,
        )

        return True

    def reject_clip(
        self,
        clip_id: str,
        actual_text: str = "",
        reason: str = "",
    ) -> bool:
        """Mark a clip's correction as rejected (wrong).

        This flags the correction as an error for learning.
        """
        clip = self._clips.get(clip_id)
        if not clip:
            return False

        clip.status = "rejected"
        clip.processed_at = datetime.now(UTC).isoformat()

        # Update state
        if clip_id in self._state.pending_clips:
            self._state.pending_clips.remove(clip_id)
        self._state.rejected_today += 1
        self._state.reviewed_today += 1

        self._save_clips()
        self._save_state()

        # Trigger learning and downstream propagation
        self._trigger_learning(
            clip.correction_id,
            approved=False,
            actual_text=actual_text,
            reason=reason,
            clip_id=clip_id,
        )

        return True

    def _trigger_learning(
        self,
        correction_id: str,
        approved: bool,
        actual_text: str = "",
        reason: str = "",
        clip_id: str = "",
    ) -> None:
        """Trigger the correction learning pipeline and downstream propagation.

        Integrates with:
        1. correction_review system (training data)
        2. correction_propagation system (glossary, RAG, notifications)
        """
        user = os.environ.get("USER", "guided_review")

        # Get correction details for propagation
        correction_data = self._get_correction_data(correction_id)

        # 1. Update training data via correction_review
        try:
            from .correction_review import CorrectionReviewSystem

            review_system = CorrectionReviewSystem()

            if approved:
                review_system.confirm_correct(
                    correction_id=correction_id,
                    confirmed_by=user,
                    note="Approved via guided audio review",
                )
            else:
                review_system.flag_error(
                    correction_id=correction_id,
                    flagged_by=user,
                    actual_correct=actual_text or "unknown",
                    reason=reason or "Flagged during guided audio review",
                )
        except ImportError:
            pass  # Correction review system not available

        # 2. Propagate to downstream systems (glossary, RAG, notifications)
        try:
            from .correction_propagation import CorrectionPropagator

            propagator = CorrectionPropagator()

            if approved:
                propagator.propagate_approval(
                    correction_id=correction_id,
                    original=correction_data.get("original", ""),
                    corrected=correction_data.get("corrected", ""),
                    category=correction_data.get("category", "technical_term"),
                    signal_ids=correction_data.get("signal_ids", []),
                    clip_id=clip_id,
                    confirmed_by=user,
                )
            else:
                propagator.propagate_rejection(
                    correction_id=correction_id,
                    original=correction_data.get("original", ""),
                    suggested=correction_data.get("corrected", ""),
                    actual_correct=actual_text or correction_data.get("original", ""),
                    category=correction_data.get("category", "technical_term"),
                    signal_ids=correction_data.get("signal_ids", []),
                    published_endpoints=correction_data.get("published_endpoints", []),
                    clip_id=clip_id,
                    flagged_by=user,
                    reason=reason,
                )
        except ImportError:
            pass  # Propagation system not available

    def _get_correction_data(self, correction_id: str) -> dict:
        """Get correction details from the applied corrections log."""
        try:
            from .correction_review import CorrectionReviewSystem

            system = CorrectionReviewSystem()
            correction = system.get_applied(correction_id)

            if correction:
                return {
                    "original": correction.original,
                    "corrected": correction.corrected,
                    "category": correction.category,
                    "signal_ids": correction.signal_ids,
                    "published_endpoints": correction.published_endpoints,
                }
        except Exception:
            pass

        return {}

    def cleanup_processed_clips(self) -> dict:
        """Remove clips that have been processed and are past retention.

        Returns:
            Stats about cleanup
        """
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=self.CLIP_RETENTION_DAYS)

        stats = {
            "checked": 0,
            "removed": 0,
            "kept": 0,
            "bytes_freed": 0,
        }

        to_remove = []

        for clip_id, clip in self._clips.items():
            stats["checked"] += 1

            # Only clean up processed clips (approved/rejected)
            if clip.status not in ("approved", "rejected"):
                stats["kept"] += 1
                continue

            # Check if past retention period
            if clip.processed_at:
                processed_time = datetime.fromisoformat(clip.processed_at)
                if processed_time < cutoff:
                    to_remove.append(clip_id)

                    # Delete the actual clip file
                    if clip.clip_path:
                        clip_path = Path(clip.clip_path)
                        if clip_path.exists():
                            stats["bytes_freed"] += clip_path.stat().st_size
                            clip_path.unlink()

                    stats["removed"] += 1
                else:
                    stats["kept"] += 1
            else:
                stats["kept"] += 1

        # Remove from index
        for clip_id in to_remove:
            del self._clips[clip_id]

        self._save_clips()

        return stats

    def get_review_prompt(self) -> str:
        """Generate a natural language prompt for the user.

        Used by Neut during conversations.
        """
        pending = self.get_pending_corrections()
        if not pending:
            return ""

        count = len(pending)

        # Vary the prompt based on context
        if self._state.deferral_count == 0:
            return (
                f"I have {count} correction(s) from recent voice memos. "
                f"Would you like to review them now? It should take about "
                f"{count * 15} seconds."
            )
        elif self._state.deferral_count < 3:
            return (
                f"Reminder: {count} correction(s) still pending review. "
                f"Quick review now, or defer again?"
            )
        else:
            return (
                f"You've deferred correction review {self._state.deferral_count} times. "
                f"There are {count} pending. Would you like to batch-approve them, "
                f"or review a few now?"
            )

    def guided_review_session(
        self,
        limit: int = 5,
    ) -> list[dict]:
        """Start a guided review session.

        Returns list of corrections to review with their audio clips.

        Args:
            limit: Max corrections to review in this session

        Returns:
            List of correction data for interactive review
        """
        pending = self.get_pending_corrections()[:limit]

        session = []
        for clip in pending:
            session.append({
                "clip_id": clip.clip_id,
                "correction_id": clip.correction_id,
                "transcript": clip.transcript_segment,
                "audio_clip": clip.clip_path or f"{clip.source_audio_path} @ {clip.start_time_sec:.1f}s",
                "duration": f"{clip.duration_sec:.1f}s",
                "created": clip.created_at,
            })

        # Update session tracking
        self._state.last_review = datetime.now(UTC).isoformat()
        self._state.deferral_count = 0  # Reset deferral count on actual review
        self._save_state()

        return session
