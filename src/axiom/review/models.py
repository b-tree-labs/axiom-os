# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Review data models and session persistence.

Core types:
    ReviewDecision  — One reviewer's verdict on one item (channel-aware).
    ReviewItem      — One thing for a human to review.
    ReviewSession   — Persistent state for a review session.
    ReviewSessionStore — JSON persistence for review sessions.

Design notes:
    * Multi-reviewer: each item collects decisions from many reviewers.
    * Channel-agnostic: decisions record *how* the review arrived (cli,
      email, teams, slack) so the same session can be reviewed across
      channels.
    * Consensus modes: ``any`` (first reviewer wins), ``all`` (every
      listed reviewer must decide), ``majority`` (>50 % agree).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from axiom.infra.hash_utils import LONG, fingerprint

# ── helpers ──────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _source_hash(content: str) -> str:
    """Deterministic hash of source content for change detection."""
    return fingerprint(content, length=LONG)


# ── data classes ─────────────────────────────────────────────────────

@dataclass
class ReviewDecision:
    """One reviewer's verdict on one item."""

    reviewer: str
    status: str  # accepted / edited / rejected / skipped
    channel: str = "cli"  # cli / email / teams / slack
    edited_content: str = ""
    comment: str = ""
    decided_at: str = ""

    def to_dict(self) -> dict:
        return {
            "reviewer": self.reviewer,
            "status": self.status,
            "channel": self.channel,
            "edited_content": self.edited_content,
            "comment": self.comment,
            "decided_at": self.decided_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ReviewDecision:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ReviewItem:
    """One thing for a human to review."""

    item_id: str
    heading: str  # short label shown in "[1/N] {heading}"
    content: str  # main display content
    context: str = ""  # optional extra context
    status: str = "pending"  # aggregate: pending / accepted / edited / rejected / skipped
    decisions: list[ReviewDecision] = field(default_factory=list)
    required_reviewers: list[str] = field(default_factory=list)

    # ── consensus helpers ────────────────────────────────────────────

    def resolve_status(self, mode: str = "any") -> str:
        """Compute aggregate status from individual decisions.

        Modes:
            any      — first decision wins.
            all      — all required reviewers must decide, and all must
                       agree (else status stays pending).
            majority — >50 % of decisions must share the same status.
        """
        if not self.decisions:
            return "pending"

        if mode == "any":
            return self.decisions[-1].status

        statuses = [d.status for d in self.decisions]

        if mode == "all":
            if self.required_reviewers:
                decided = {d.reviewer for d in self.decisions}
                if not all(r in decided for r in self.required_reviewers):
                    return "pending"
            if len(set(statuses)) == 1:
                return statuses[0]
            return "pending"

        if mode == "majority":
            from collections import Counter
            counts = Counter(statuses)
            top_status, top_count = counts.most_common(1)[0]
            if top_count > len(statuses) / 2:
                return top_status
            return "pending"

        return "pending"

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "heading": self.heading,
            "content": self.content,
            "context": self.context,
            "status": self.status,
            "decisions": [d.to_dict() for d in self.decisions],
            "required_reviewers": self.required_reviewers,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ReviewItem:
        decisions = [ReviewDecision.from_dict(d) for d in data.get("decisions", [])]
        return cls(
            item_id=data.get("item_id", ""),
            heading=data.get("heading", ""),
            content=data.get("content", ""),
            context=data.get("context", ""),
            status=data.get("status", "pending"),
            decisions=decisions,
            required_reviewers=data.get("required_reviewers", []),
        )


@dataclass
class ReviewSession:
    """Persistent state for a review session."""

    session_id: str
    session_type: str  # "draft", "correction", "action", etc.
    source: str  # file path, batch ID, etc.
    source_hash: str  # detect changes since last session
    started_at: str
    items: list[ReviewItem] = field(default_factory=list)
    last_reviewed_at: str = ""
    reviewers: list[str] = field(default_factory=list)
    consensus_mode: str = "any"  # any / all / majority

    # ── convenience ──────────────────────────────────────────────────

    @property
    def pending_items(self) -> list[ReviewItem]:
        return [i for i in self.items if i.status == "pending"]

    @property
    def reviewed_items(self) -> list[ReviewItem]:
        return [i for i in self.items if i.status != "pending"]

    @property
    def progress(self) -> tuple[int, int]:
        """Return (reviewed, total)."""
        return len(self.reviewed_items), len(self.items)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "session_type": self.session_type,
            "source": self.source,
            "source_hash": self.source_hash,
            "started_at": self.started_at,
            "last_reviewed_at": self.last_reviewed_at,
            "reviewers": self.reviewers,
            "consensus_mode": self.consensus_mode,
            "items": [i.to_dict() for i in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict) -> ReviewSession:
        items = [ReviewItem.from_dict(i) for i in data.get("items", [])]
        return cls(
            session_id=data.get("session_id", ""),
            session_type=data.get("session_type", ""),
            source=data.get("source", ""),
            source_hash=data.get("source_hash", ""),
            started_at=data.get("started_at", ""),
            last_reviewed_at=data.get("last_reviewed_at", ""),
            reviewers=data.get("reviewers", []),
            consensus_mode=data.get("consensus_mode", "any"),
            items=items,
        )


# ── persistence ──────────────────────────────────────────────────────

class ReviewSessionStore:
    """JSON persistence for review sessions.

    Default path: ``.neut/review_state.json`` (alongside setup-state.json
    and update-state.json, already gitignored).
    """

    def __init__(self, state_path: Path | None = None):
        if state_path is None:
            # Default to .neut/ in repo root
            from axiom import REPO_ROOT
            state_path = REPO_ROOT / ".neut" / "review_state.json"
        self.path = state_path
        self._sessions: dict[str, ReviewSession] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for item in data.get("sessions", []):
                session = ReviewSession.from_dict(item)
                self._sessions[session.session_id] = session
        except (json.JSONDecodeError, KeyError):
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "sessions": [s.to_dict() for s in self._sessions.values()],
        }
        self.path.write_text(
            json.dumps(data, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def get(self, session_id: str) -> ReviewSession | None:
        return self._sessions.get(session_id)

    def save(self, session: ReviewSession) -> None:
        self._sessions[session.session_id] = session
        self._save()

    def remove(self, session_id: str) -> bool:
        if session_id in self._sessions:
            del self._sessions[session_id]
            self._save()
            return True
        return False

    def list_active(self) -> list[ReviewSession]:
        """Return sessions that still have pending items."""
        return [s for s in self._sessions.values() if s.pending_items]

    def list_all(self) -> list[ReviewSession]:
        return list(self._sessions.values())

    def find_by_source(self, source: str) -> ReviewSession | None:
        """Find the most recent session for a given source."""
        matches = [s for s in self._sessions.values() if s.source == source]
        if not matches:
            return None
        return max(matches, key=lambda s: s.started_at)
