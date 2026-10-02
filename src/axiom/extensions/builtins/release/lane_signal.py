# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET's half of `lane`: say which lane a merge belonged to.

ADR-046: *RIVET makes the green; TIDY removes the brown. They meet only at
the event bus.* RIVET is the authoritative signal of merge and ship state
and performs **no destructive git operations**. So its entire contribution
to `lane` is naming: when a branch merges, say which lane was on it, so the
event TIDY already receives carries the lane instead of making TIDY infer it.

Inference is exactly what should not happen here. TIDY guessing which lane a
merged branch belonged to — by matching a slug, or a directory name that may
since have changed — is how the wrong lane gets proposed for reclamation.
The registry knows. RIVET reads it and attaches the answer.

This module decides nothing and deletes nothing. It is a lookup with a
reason for existing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LaneRef:
    """The lane a signal is about, if any."""

    name: str
    database: str = ""
    isolated: bool = False

    def as_event_fields(self) -> dict:
        """What rides along on `rivet.pr_merged` / `rivet.tag_released`."""
        return {"lane": self.name, "lane_database": self.database or None,
                "lane_isolated": self.isolated}


def lane_for_branch(branch: str, lanes: dict) -> LaneRef | None:
    """The lane claimed on `branch`, or None.

    Exact match on the recorded branch only. A near-match is worse than no
    match: acting on the wrong lane is the failure, and "no lane" is a
    perfectly good answer that costs nothing.
    """
    if not branch:
        return None
    for name, lane in sorted(lanes.items()):
        if getattr(lane, "branch", "") == branch:
            return LaneRef(name=name, database=getattr(lane, "database", "") or "",
                           isolated=bool(getattr(lane, "isolated", False)))
    return None


def annotate(event: dict, lanes: dict) -> dict:
    """Return `event` with lane fields added when its branch names one.

    Non-destructive and total: an event about a branch with no lane comes
    back unchanged rather than gaining empty keys, so a consumer can tell
    "no lane" from "lane unknown".
    """
    ref = lane_for_branch(str(event.get("branch") or ""), lanes)
    return {**event, **ref.as_event_fields()} if ref else dict(event)


__all__ = ["LaneRef", "annotate", "lane_for_branch"]
