# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Data models for the signal ingestion pipeline."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from axiom.infra.state import atomic_write

# Standard date formats for consistency across the pipeline
DATE_FORMAT_ISO = "%Y-%m-%d"  # User-facing filenames: changelog_2026-02-24.md
DATETIME_FORMAT_COMPACT = "%Y%m%d_%H%M%S"  # Internal IDs: signal_20260224_143052


def _compute_signal_hash(source: str, timestamp: str, detail: str, raw_text: str) -> str:
    """Compute a stable hash for signal deduplication.

    Uses source + timestamp + first 500 chars of raw_text + detail.
    Returns first 12 chars of SHA256 for readability.
    """
    content = f"{source}|{timestamp}|{raw_text[:500]}|{detail}"
    return hashlib.sha256(content.encode()).hexdigest()[:12]


@dataclass
class Signal:
    """A structured signal extracted from any source.

    Every extractor produces a list of Signal objects. These flow through
    the correlator (name/initiative resolution) and synthesizer (changelog
    generation).
    """

    source: str  # "gitlab_diff", "voice", "transcript", "freetext"
    timestamp: str  # ISO 8601
    raw_text: str  # Original content (or excerpt)
    people: list[str] = field(default_factory=list)
    initiatives: list[str] = field(default_factory=list)
    signal_type: str = "raw"  # progress, blocker, decision, action_item, status_change, raw
    detail: str = ""  # Human-readable summary
    confidence: float = 0.5  # 0.0-1.0 (1.0 for gitlab_diff, lower for LLM)
    metadata: dict = field(default_factory=dict)

    # Provenance: who submitted this signal (for feedback loop)
    originator: str = ""  # Email or identifier of signal creator
    originator_notified: bool = False  # Have we sent them a receipt?
    feedback_received: bool = False  # Have they responded?

    @property
    def signal_id(self) -> str:
        """Unique identifier for deduplication, computed from content hash."""
        return _compute_signal_hash(self.source, self.timestamp, self.detail, self.raw_text)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "timestamp": self.timestamp,
            "raw_text": self.raw_text,
            "people": self.people,
            "initiatives": self.initiatives,
            "signal_type": self.signal_type,
            "detail": self.detail,
            "confidence": self.confidence,
            "metadata": self.metadata,
            "originator": self.originator,
            "originator_notified": self.originator_notified,
            "feedback_received": self.feedback_received,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Signal:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Extraction:
    """Result of running an extractor on a source file."""

    extractor: str  # Name of the extractor that produced this
    source_file: str  # Path to input file
    signals: list[Signal] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    extracted_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )

    def to_dict(self) -> dict:
        return {
            "extractor": self.extractor,
            "source_file": self.source_file,
            "signals": [s.to_dict() for s in self.signals],
            "errors": self.errors,
            "extracted_at": self.extracted_at,
        }


@dataclass
class ChangelogEntry:
    """A single entry in a generated changelog."""

    initiative: str
    signal_type: str
    detail: str
    people: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    confidence: float = 1.0


@dataclass
class Changelog:
    """A synthesized changelog draft, ready for human review."""

    date: str
    entries: list[ChangelogEntry] = field(default_factory=list)
    summary: str = ""
    generated_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )


@dataclass
class ReportedSignal:
    """Record of a signal that has been included in a changelog."""

    signal_id: str
    reported_at: str
    changelog_date: str
    initiative: str = ""
    detail_preview: str = ""  # First 100 chars for human review


class SignalManifest:
    """Tracks which signals have been reported to avoid re-reporting stale news.

    Persists to signal_manifest.json in the processed directory.
    """

    def __init__(self, manifest_path: Path | None = None):
        from axiom import REPO_ROOT as _REPO_ROOT
        self.manifest_path = manifest_path or (_REPO_ROOT / "runtime" / "inbox" / "processed" / "signal_manifest.json")
        self._reported: dict[str, ReportedSignal] = {}
        self._load()

    def _load(self) -> None:
        """Load manifest from disk."""
        import json
        if self.manifest_path.exists():
            try:
                data = json.loads(self.manifest_path.read_text())
                for signal_id, info in data.get("reported", {}).items():
                    self._reported[signal_id] = ReportedSignal(
                        signal_id=signal_id,
                        reported_at=info.get("reported_at", ""),
                        changelog_date=info.get("changelog_date", ""),
                        initiative=info.get("initiative", ""),
                        detail_preview=info.get("detail_preview", ""),
                    )
            except (json.JSONDecodeError, KeyError):
                self._reported = {}

    def _save(self) -> None:
        """Persist manifest to disk."""
        data = {
            "reported": {
                sig_id: {
                    "reported_at": r.reported_at,
                    "changelog_date": r.changelog_date,
                    "initiative": r.initiative,
                    "detail_preview": r.detail_preview,
                }
                for sig_id, r in self._reported.items()
            },
            "last_updated": datetime.now(UTC).isoformat(),
        }
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.manifest_path, data)

    def is_reported(self, signal: Signal) -> bool:
        """Check if a signal has already been reported."""
        return signal.signal_id in self._reported

    def mark_reported(self, signal: Signal, changelog_date: str) -> None:
        """Mark a signal as reported in the given changelog."""
        self._reported[signal.signal_id] = ReportedSignal(
            signal_id=signal.signal_id,
            reported_at=datetime.now(UTC).isoformat(),
            changelog_date=changelog_date,
            initiative=signal.initiatives[0] if signal.initiatives else "",
            detail_preview=signal.detail[:100],
        )

    def mark_batch_reported(self, signals: list[Signal], changelog_date: str) -> None:
        """Mark multiple signals as reported and save."""
        for signal in signals:
            self.mark_reported(signal, changelog_date)
        self._save()

    def filter_unreported(self, signals: list[Signal]) -> list[Signal]:
        """Return only signals that haven't been reported yet."""
        return [s for s in signals if not self.is_reported(s)]

    def get_reported_count(self) -> int:
        """Return total number of reported signals."""
        return len(self._reported)

    def get_reported_since(self, date: str) -> list[ReportedSignal]:
        """Get signals reported since a given date."""
        return [r for r in self._reported.values() if r.changelog_date >= date]
