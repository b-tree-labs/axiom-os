# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Echo Suppression — prevents signal feedback loops.

Problem:
    Voice memo → Signal → Briefing → Read aloud in meeting → Transcript → Signal
    The same information comes back as a "new" signal = echo/feedback loop

Solution:
    1. Track lineage: which signals derived from which
    2. Index published content: know what we've already said
    3. Detect echoes: fuzzy matching against published content
    4. Suppress: mark echoes so they don't re-enter the pipeline

Usage:
    from axiom.extensions.builtins.signals.echo_suppression import EchoSuppressor

    suppressor = EchoSuppressor()

    # When publishing a briefing
    suppressor.record_published(signals, output_text, "briefing_2024-01-15")

    # When ingesting new signals
    for signal in new_signals:
        if suppressor.is_echo(signal):
            signal.metadata["echo_of"] = suppressor.get_echo_source(signal)
            continue  # Skip or flag

        process_signal(signal)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.hash_utils import LONG, fingerprint
from axiom.infra.time_utils import parse_iso

from .models import Signal

_RUNTIME_DIR = _REPO_ROOT / "runtime"
ECHO_INDEX_DIR = _RUNTIME_DIR / "inbox" / "state" / "echo_index"


def _normalize_text(text: str) -> str:
    """Normalize text for comparison (lowercase, collapse whitespace, remove punctuation)."""
    text = text.lower()
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _compute_content_hash(text: str) -> str:
    """Hash normalized text content."""
    normalized = _normalize_text(text)
    return fingerprint(normalized, length=LONG)


def _extract_key_phrases(text: str, min_length: int = 3) -> set[str]:
    """Extract key phrases (n-grams) for fuzzy matching.

    Uses 3-word sliding window to capture key phrases that might appear
    verbatim when content is read aloud.
    """
    words = _normalize_text(text).split()
    phrases = set()

    # 3-grams for phrase matching
    for i in range(len(words) - 2):
        phrase = ' '.join(words[i:i+3])
        if len(phrase) >= min_length:
            phrases.add(phrase)

    # Also include individual significant words (5+ chars)
    for word in words:
        if len(word) >= 5:
            phrases.add(word)

    return phrases


@dataclass
class PublishedContent:
    """Record of content we've published (briefings, digests, etc.)."""

    id: str                     # Unique ID (e.g., "briefing_2024-01-15")
    content_type: str           # "briefing", "digest", "slack_post", etc.
    published_at: str           # ISO timestamp

    # The actual published text
    content_text: str
    content_hash: str           # Hash of normalized content

    # Source signals that contributed to this content
    source_signal_ids: list[str] = field(default_factory=list)

    # Key phrases for fuzzy matching
    key_phrases: list[str] = field(default_factory=list)

    # Expiry: don't check against ancient content
    expires_at: str = ""        # ISO timestamp, empty = never

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content_type": self.content_type,
            "published_at": self.published_at,
            "content_text": self.content_text,
            "content_hash": self.content_hash,
            "source_signal_ids": self.source_signal_ids,
            "key_phrases": self.key_phrases,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PublishedContent:
        return cls(**data)


@dataclass
class EchoMatch:
    """Result of echo detection."""

    is_echo: bool
    confidence: float           # 0.0-1.0
    match_type: str             # "exact", "phrase", "semantic", "none"
    matched_content_id: str     # ID of the published content that matches
    matched_phrases: list[str]  # Which phrases matched
    explanation: str


class EchoSuppressor:
    """Detects and suppresses echo/feedback loops in the signal pipeline."""

    # Thresholds
    EXACT_MATCH_THRESHOLD = 0.95      # Hash match = definitely echo
    PHRASE_MATCH_THRESHOLD = 0.3      # 30% of phrases match = likely echo
    MIN_PHRASES_FOR_MATCH = 3         # Need at least 3 phrase matches

    # How long to check against published content
    DEFAULT_LOOKBACK_DAYS = 14

    def __init__(self, index_dir: Path | None = None):
        self.index_dir = index_dir or ECHO_INDEX_DIR
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self._published_index: dict[str, PublishedContent] = {}
        self._phrase_index: dict[str, set[str]] = {}  # phrase -> content_ids
        self._hash_index: dict[str, str] = {}         # hash -> content_id

        self._load_index()

    # ---------------------------------------------------------------------------
    # Publishing: Record what we've output
    # ---------------------------------------------------------------------------

    def record_published(
        self,
        signals: list[Signal],
        output_text: str,
        content_id: str,
        content_type: str = "briefing",
        expires_days: int = DEFAULT_LOOKBACK_DAYS,
    ) -> PublishedContent:
        """Record published content so we can detect echoes of it later.

        Call this when generating briefings, digests, Slack posts, etc.

        Args:
            signals: Source signals that contributed to this content
            output_text: The actual published text
            content_id: Unique ID for this content
            content_type: Type of content (briefing, digest, etc.)
            expires_days: How long to check for echoes of this content
        """
        content_hash = _compute_content_hash(output_text)
        key_phrases = list(_extract_key_phrases(output_text))

        expires_at = ""
        if expires_days > 0:
            expires_at = (
                datetime.now(UTC) + timedelta(days=expires_days)
            ).isoformat()

        published = PublishedContent(
            id=content_id,
            content_type=content_type,
            published_at=datetime.now(UTC).isoformat(),
            content_text=output_text,
            content_hash=content_hash,
            source_signal_ids=[s.signal_id for s in signals],
            key_phrases=key_phrases,
            expires_at=expires_at,
        )

        # Add to indexes
        self._published_index[content_id] = published
        self._hash_index[content_hash] = content_id

        for phrase in key_phrases:
            if phrase not in self._phrase_index:
                self._phrase_index[phrase] = set()
            self._phrase_index[phrase].add(content_id)

        self._save_index()

        return published

    # ---------------------------------------------------------------------------
    # Detection: Check if a signal is an echo
    # ---------------------------------------------------------------------------

    def is_echo(self, signal: Signal, lookback_days: int | None = None) -> bool:
        """Check if a signal is an echo of previously published content."""
        return self.detect_echo(signal, lookback_days).is_echo

    def detect_echo(
        self,
        signal: Signal,
        lookback_days: int | None = None,
    ) -> EchoMatch:
        """Detect if a signal is an echo, with detailed match info.

        Args:
            signal: Signal to check
            lookback_days: How far back to check (None = use content expiry)

        Returns:
            EchoMatch with detection results
        """
        lookback = lookback_days or self.DEFAULT_LOOKBACK_DAYS
        cutoff = datetime.now(UTC) - timedelta(days=lookback)

        # Get text to check
        text = signal.raw_text + " " + signal.detail

        # 1. Check exact hash match
        content_hash = _compute_content_hash(text)
        if content_hash in self._hash_index:
            content_id = self._hash_index[content_hash]
            content = self._published_index.get(content_id)
            if content and self._is_valid(content, cutoff):
                return EchoMatch(
                    is_echo=True,
                    confidence=0.98,
                    match_type="exact",
                    matched_content_id=content_id,
                    matched_phrases=[],
                    explanation=f"Exact content match with {content.content_type} '{content_id}'",
                )

        # 2. Check phrase overlap
        signal_phrases = _extract_key_phrases(text)

        # Count matches per published content
        content_matches: dict[str, list[str]] = {}

        for phrase in signal_phrases:
            if phrase in self._phrase_index:
                for content_id in self._phrase_index[phrase]:
                    content = self._published_index.get(content_id)
                    if content and self._is_valid(content, cutoff):
                        if content_id not in content_matches:
                            content_matches[content_id] = []
                        content_matches[content_id].append(phrase)

        # Find best match
        best_match_id = None
        best_match_phrases: list[str] = []
        best_match_ratio = 0.0

        for content_id, phrases in content_matches.items():
            content = self._published_index[content_id]
            # Ratio = matched phrases / content's total phrases
            if content.key_phrases:
                ratio = len(phrases) / len(content.key_phrases)
                if ratio > best_match_ratio:
                    best_match_ratio = ratio
                    best_match_id = content_id
                    best_match_phrases = phrases

        if (
            best_match_id
            and best_match_ratio >= self.PHRASE_MATCH_THRESHOLD
            and len(best_match_phrases) >= self.MIN_PHRASES_FOR_MATCH
        ):
            content = self._published_index[best_match_id]
            return EchoMatch(
                is_echo=True,
                confidence=min(0.9, best_match_ratio + 0.3),
                match_type="phrase",
                matched_content_id=best_match_id,
                matched_phrases=best_match_phrases[:10],  # Limit for readability
                explanation=(
                    f"Phrase match ({len(best_match_phrases)} phrases, "
                    f"{best_match_ratio:.0%} overlap) with {content.content_type} '{best_match_id}'"
                ),
            )

        # 3. Check if signal matches source signals of published content
        for content_id, content in self._published_index.items():
            if not self._is_valid(content, cutoff):
                continue
            if signal.signal_id in content.source_signal_ids:
                return EchoMatch(
                    is_echo=True,
                    confidence=0.85,
                    match_type="lineage",
                    matched_content_id=content_id,
                    matched_phrases=[],
                    explanation=f"Signal was source for {content.content_type} '{content_id}'",
                )

        # No echo detected
        return EchoMatch(
            is_echo=False,
            confidence=0.0,
            match_type="none",
            matched_content_id="",
            matched_phrases=[],
            explanation="No echo detected",
        )

    def get_echo_source(self, signal: Signal) -> str | None:
        """Get the ID of the published content this signal echoes."""
        match = self.detect_echo(signal)
        return match.matched_content_id if match.is_echo else None

    def _is_valid(self, content: PublishedContent, cutoff: datetime) -> bool:
        """Check if content is still valid for echo detection."""
        # Check expiry
        if content.expires_at:
            expires = parse_iso(content.expires_at)
            if expires < datetime.now(UTC):
                return False

        # Check lookback
        published = parse_iso(content.published_at)
        return published >= cutoff

    # ---------------------------------------------------------------------------
    # Filtering: Batch operations
    # ---------------------------------------------------------------------------

    def filter_echoes(
        self,
        signals: list[Signal],
        mark_only: bool = False,
    ) -> tuple[list[Signal], list[Signal]]:
        """Filter signals, separating echoes from non-echoes.

        Args:
            signals: Signals to filter
            mark_only: If True, mark echoes in metadata but include all

        Returns:
            (non_echoes, echoes) tuple
        """
        non_echoes = []
        echoes = []

        for signal in signals:
            match = self.detect_echo(signal)

            if match.is_echo:
                signal.metadata["is_echo"] = True
                signal.metadata["echo_of"] = match.matched_content_id
                signal.metadata["echo_confidence"] = match.confidence
                signal.metadata["echo_type"] = match.match_type
                echoes.append(signal)

                if mark_only:
                    non_echoes.append(signal)
            else:
                non_echoes.append(signal)

        return non_echoes, echoes

    def get_echo_stats(self) -> dict:
        """Get statistics about echo suppression."""
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=self.DEFAULT_LOOKBACK_DAYS)

        active = 0
        expired = 0
        by_type: dict[str, int] = {}

        for content in self._published_index.values():
            if self._is_valid(content, cutoff):
                active += 1
                by_type[content.content_type] = by_type.get(content.content_type, 0) + 1
            else:
                expired += 1

        return {
            "active_published": active,
            "expired_published": expired,
            "total_phrases_indexed": len(self._phrase_index),
            "by_type": by_type,
        }

    # ---------------------------------------------------------------------------
    # Persistence
    # ---------------------------------------------------------------------------

    def _load_index(self) -> None:
        """Load index from disk."""
        index_file = self.index_dir / "published_content.jsonl"

        if not index_file.exists():
            return

        with open(index_file) as f:
            for line in f:
                if not line.strip():
                    continue
                data = json.loads(line)
                content = PublishedContent.from_dict(data)
                self._published_index[content.id] = content
                self._hash_index[content.content_hash] = content.id

                for phrase in content.key_phrases:
                    if phrase not in self._phrase_index:
                        self._phrase_index[phrase] = set()
                    self._phrase_index[phrase].add(content.id)

    def _save_index(self) -> None:
        """Save index to disk (atomic full-file rewrite, multi-process safe)."""
        import os
        import tempfile
        index_file = self.index_dir / "published_content.jsonl"
        index_file.parent.mkdir(parents=True, exist_ok=True)
        lines = "\n".join(
            json.dumps(c.to_dict(), default=str, sort_keys=True)
            for c in self._published_index.values()
        ) + ("\n" if self._published_index else "")
        fd, tmp = tempfile.mkstemp(dir=str(index_file.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(lines)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, str(index_file))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def cleanup_expired(self) -> int:
        """Remove expired content from index."""
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=self.DEFAULT_LOOKBACK_DAYS)

        expired_ids = [
            content_id
            for content_id, content in self._published_index.items()
            if not self._is_valid(content, cutoff)
        ]

        for content_id in expired_ids:
            content = self._published_index.pop(content_id)

            # Remove from hash index
            if content.content_hash in self._hash_index:
                del self._hash_index[content.content_hash]

            # Remove from phrase index
            for phrase in content.key_phrases:
                if phrase in self._phrase_index:
                    self._phrase_index[phrase].discard(content_id)
                    if not self._phrase_index[phrase]:
                        del self._phrase_index[phrase]

        if expired_ids:
            self._save_index()

        return len(expired_ids)


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------

_suppressor: EchoSuppressor | None = None


def get_suppressor() -> EchoSuppressor:
    """Get singleton suppressor instance."""
    global _suppressor
    if _suppressor is None:
        _suppressor = EchoSuppressor()
    return _suppressor


def record_published(
    signals: list[Signal],
    output_text: str,
    content_id: str,
    content_type: str = "briefing",
) -> PublishedContent:
    """Record published content for echo detection."""
    return get_suppressor().record_published(
        signals, output_text, content_id, content_type
    )


def filter_echoes(signals: list[Signal]) -> tuple[list[Signal], list[Signal]]:
    """Filter echoes from a list of signals."""
    return get_suppressor().filter_echoes(signals)


def is_echo(signal: Signal) -> bool:
    """Check if a signal is an echo."""
    return get_suppressor().is_echo(signal)
