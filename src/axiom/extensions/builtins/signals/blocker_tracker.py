# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""BlockerTracker — persistent tracking of blockers across synthesis cycles.

Identifies cross-cutting blockers (affecting 2+ initiatives), tracks recurrence,
and proposes next steps. State persists to blocker_state.json.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.state import atomic_write

from .models import DATE_FORMAT_ISO, Signal

_RUNTIME_DIR = _REPO_ROOT / "runtime"
DEFAULT_STATE_PATH = _RUNTIME_DIR / "inbox" / "state" / "blocker_state.json"


def _blocker_id(detail: str) -> str:
    """Compute a stable hash for a blocker based on its detail text."""
    normalized = detail.lower().strip()
    return hashlib.sha256(normalized.encode()).hexdigest()[:12]


@dataclass
class TrackedBlocker:
    """A blocker tracked across synthesis cycles."""

    blocker_id: str
    detail: str
    initiatives: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""
    times_reported: int = 1
    status: str = "open"  # open / resolved / stale
    is_cross_cutting: bool = False
    proposed_action: str = ""  # create_issue, send_followup, etc.

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> TrackedBlocker:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class BlockerTracker:
    """Track blockers across synthesis cycles with persistence.

    Pattern: follows SignalManifest in models.py (JSON load/save).
    """

    def __init__(self, state_path: Path | None = None):
        self.state_path = state_path or DEFAULT_STATE_PATH
        self._blockers: dict[str, TrackedBlocker] = {}
        self._load()

    def _load(self) -> None:
        """Load blocker state from disk."""
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text())
                for bid, info in data.get("blockers", {}).items():
                    self._blockers[bid] = TrackedBlocker.from_dict(info)
            except (json.JSONDecodeError, KeyError):
                self._blockers = {}

    def _save(self) -> None:
        """Persist blocker state to disk."""
        data = {
            "blockers": {
                bid: b.to_dict() for bid, b in self._blockers.items()
            },
            "last_updated": datetime.now(UTC).isoformat(),
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.state_path, data)

    def update(self, blocker_signals: list[Signal]) -> None:
        """Update tracker with new blocker signals from the current cycle.

        For each blocker signal:
        - If a matching blocker exists, increment times_reported and update last_seen.
        - If new, create a TrackedBlocker entry.

        After processing all signals, detect cross-cutting blockers.
        """
        today = datetime.now(UTC).strftime(DATE_FORMAT_ISO)

        for signal in blocker_signals:
            if signal.signal_type != "blocker":
                continue

            bid = _blocker_id(signal.detail)

            if bid in self._blockers:
                existing = self._blockers[bid]
                existing.times_reported += 1
                existing.last_seen = today
                # Merge new initiatives/people
                for init in signal.initiatives:
                    if init not in existing.initiatives:
                        existing.initiatives.append(init)
                for person in signal.people:
                    if person not in existing.people:
                        existing.people.append(person)
            else:
                self._blockers[bid] = TrackedBlocker(
                    blocker_id=bid,
                    detail=signal.detail,
                    initiatives=list(signal.initiatives),
                    people=list(signal.people),
                    first_seen=today,
                    last_seen=today,
                    times_reported=1,
                    status="open",
                )

        self._detect_cross_cutting()
        self._propose_actions()
        self._save()

    def _detect_cross_cutting(self) -> None:
        """Mark blockers that affect 2+ initiatives as cross-cutting."""
        for blocker in self._blockers.values():
            if blocker.status != "open":
                continue
            blocker.is_cross_cutting = len(blocker.initiatives) >= 2

    def _propose_actions(self) -> None:
        """Set proposed_action based on blocker characteristics."""
        for blocker in self._blockers.values():
            if blocker.status != "open":
                continue
            if blocker.is_cross_cutting:
                blocker.proposed_action = "create_issue"
            elif blocker.people:
                blocker.proposed_action = "send_followup"
            else:
                blocker.proposed_action = "investigate"

    def get_active_blockers(self) -> list[TrackedBlocker]:
        """Return all open blockers, cross-cutting first, then by recurrence."""
        active = [b for b in self._blockers.values() if b.status == "open"]
        active.sort(key=lambda b: (not b.is_cross_cutting, -b.times_reported))
        return active

    def get_cross_cutting_blockers(self) -> list[TrackedBlocker]:
        """Return only blockers affecting 2+ initiatives."""
        return [
            b for b in self._blockers.values()
            if b.status == "open" and b.is_cross_cutting
        ]

    def resolve_blocker(self, blocker_id: str) -> bool:
        """Mark a blocker as resolved. Returns True if found."""
        if blocker_id in self._blockers:
            self._blockers[blocker_id].status = "resolved"
            self._save()
            return True
        return False

    def age_stale_blockers(self, stale_after_cycles: int = 5) -> int:
        """Mark blockers as stale if they haven't been reported recently.

        Returns count of newly staled blockers.
        """
        staled = 0
        for blocker in self._blockers.values():
            if blocker.status == "open" and blocker.times_reported >= stale_after_cycles:
                blocker.status = "stale"
                staled += 1
        if staled:
            self._save()
        return staled
