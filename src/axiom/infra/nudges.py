# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""NudgeStore — persistent lightweight reminders for deferred setup tasks.

Nudges are stored at ~/<state-dir>/nudges.json. Each nudge has:
  - id: unique slug
  - message: human-readable description
  - hint: the command or action to take
  - created_at: ISO timestamp
  - snoozed_until: ISO timestamp or null
  - dismissed: bool

Usage:
    store = NudgeStore()
    store.add("ext-remote-my-tools",
              message="Add a git remote to your my-tools extension",
              hint="cd ~/<state-dir>/extensions/my-tools && git remote add origin <url>")
    store.pending()    # list of active nudges
    store.snooze("ext-remote-my-tools", days=7)
    store.dismiss("ext-remote-my-tools")
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from axiom.infra.paths import get_user_state_dir
from axiom.infra.state import atomic_write


def _get_default_path() -> Path:
    return get_user_state_dir() / "nudges.json"


class Nudge:
    def __init__(
        self,
        id: str,
        message: str,
        hint: str = "",
        created_at: str = "",
        snoozed_until: str | None = None,
        dismissed: bool = False,
    ):
        self.id = id
        self.message = message
        self.hint = hint
        self.created_at = created_at or _now()
        self.snoozed_until = snoozed_until
        self.dismissed = dismissed

    def is_active(self) -> bool:
        if self.dismissed:
            return False
        if self.snoozed_until:
            try:
                until = datetime.fromisoformat(self.snoozed_until)
                if until > datetime.now(UTC):
                    return False
            except ValueError:
                pass
        return True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "message": self.message,
            "hint": self.hint,
            "created_at": self.created_at,
            "snoozed_until": self.snoozed_until,
            "dismissed": self.dismissed,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Nudge:
        return cls(
            id=d["id"],
            message=d.get("message", ""),
            hint=d.get("hint", ""),
            created_at=d.get("created_at", ""),
            snoozed_until=d.get("snoozed_until"),
            dismissed=d.get("dismissed", False),
        )


class NudgeStore:
    def __init__(self, path: Path | None = None):
        self.path = path or _get_default_path()

    def _load(self) -> list[Nudge]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [Nudge.from_dict(d) for d in data]
        except Exception:
            return []

    def _save(self, nudges: list[Nudge]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, [n.to_dict() for n in nudges])

    def add(self, id: str, message: str, hint: str = "") -> None:
        """Add a nudge. No-op if already exists."""
        nudges = self._load()
        if any(n.id == id for n in nudges):
            return
        nudges.append(Nudge(id=id, message=message, hint=hint))
        self._save(nudges)

    def pending(self) -> list[Nudge]:
        """Return nudges that are active (not dismissed or snoozed)."""
        return [n for n in self._load() if n.is_active()]

    def snooze(self, id: str, days: int = 7) -> bool:
        """Snooze a nudge for N days. Returns True if found."""
        nudges = self._load()
        for n in nudges:
            if n.id == id:
                until = datetime.now(UTC) + timedelta(days=days)
                n.snoozed_until = until.isoformat()
                self._save(nudges)
                return True
        return False

    def dismiss(self, id: str) -> bool:
        """Permanently dismiss a nudge. Returns True if found."""
        nudges = self._load()
        for n in nudges:
            if n.id == id:
                n.dismissed = True
                self._save(nudges)
                return True
        return False

    def get(self, id: str) -> Nudge | None:
        return next((n for n in self._load() if n.id == id), None)


def _now() -> str:
    return datetime.now(UTC).isoformat()
