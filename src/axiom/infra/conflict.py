# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One vocabulary for a conflict, shared across the platform's sync engines
(ADR-112 §D3).

Two engines reconcile edits that arrive from more than one place, and each
kept its own private words for "the two sides disagreed":

- the **document mirror** (``extensions/builtins/publishing/mirror.py``) —
  a single file mirrored against a remote editor. Its policy is *preserve both
  and block*: the incoming canonical text lands in the file, your edit is kept
  in a ``.conflict`` sidecar, and sync pauses until you ``resolve``.
- **memory sync** (``memory/sync/conflict.py``) — memory fragments across
  harnesses. Its policy is *last-writer-wins by event time*: the later edit
  wins, the earlier is suppressed outbound but kept in the review queue.

The **policies stay different** — one file's bytes are not a memory fragment,
and blocking is not last-writer-wins (ADR-112 keeps that DRY floor). What they
share, and what this type carries, is the *description* a human or an agent
reads: what resource conflicted, why, where the set-aside side was kept, and
which resolutions clear it. The mirror populates it today; memory sync adopting
the same words is a documented follow-on, not a rewrite of its engine.
"""

from __future__ import annotations

from dataclasses import dataclass

# The resolutions a blocked, human-resolvable conflict offers, in the order a
# prompt should list them. "theirs" first: taking canonical is the safe default.
RESOLUTIONS: tuple[str, ...] = ("theirs", "ours", "merged")


@dataclass(frozen=True)
class ConflictOutcome:
    """A conflict between two edits of one resource, in shared words.

    ``resource`` names the thing that conflicted (a mirror name, a document
    path, a fragment-pair key). ``reason`` is a plain phrase for a person.
    ``preserved_path`` is where the side that was set aside is kept, so nothing
    is ever lost. ``incoming_version`` is the canonical version at the moment of
    the conflict. ``blocked`` says whether sync is paused until a human picks a
    side (the mirror blocks; last-writer-wins does not). ``winner`` records the
    side a policy already chose, or ``None`` while a blocked conflict waits.
    """

    resource: str
    reason: str
    preserved_path: str | None = None
    incoming_version: str = ""
    blocked: bool = True
    winner: str | None = None
    resolutions: tuple[str, ...] = RESOLUTIONS

    def guidance(self) -> str:
        """A one-line, user-facing explanation of what to do next."""
        if not self.blocked:
            won = f" ({self.winner} won)" if self.winner else ""
            return f"{self.resource}: conflict resolved{won} — {self.reason}."
        where = f" Your edit is kept at {self.preserved_path}." if self.preserved_path else ""
        picks = " | ".join(self.resolutions)
        return (
            f"{self.resource}: conflict — {self.reason}. Sync is paused until you "
            f"resolve it.{where} Choose one: {picks}."
        )

    def to_dict(self) -> dict:
        return {
            "resource": self.resource,
            "reason": self.reason,
            "preserved_path": self.preserved_path,
            "incoming_version": self.incoming_version,
            "blocked": self.blocked,
            "winner": self.winner,
            "resolutions": list(self.resolutions),
        }

    @classmethod
    def from_dict(cls, data: dict) -> ConflictOutcome:
        return cls(
            resource=data["resource"],
            reason=data.get("reason", ""),
            preserved_path=data.get("preserved_path"),
            incoming_version=data.get("incoming_version", ""),
            blocked=data.get("blocked", True),
            winner=data.get("winner"),
            resolutions=tuple(data.get("resolutions") or RESOLUTIONS),
        )


__all__ = ["ConflictOutcome", "RESOLUTIONS"]
