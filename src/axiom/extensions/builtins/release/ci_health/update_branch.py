# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Update one branch, the next to land. Never every open pull request.

Under strict up-to-date protection each merge leaves every other open pull
request behind. Updating all of them re-runs the full suite N times, and N-1
of those runs are thrown away by the next merge. RIVET updates only the pull
request it would land next; the rest wait their turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BEHIND = "BEHIND"


@dataclass(frozen=True)
class UpdatePlan:
    next: int | None
    deferred: tuple[int, ...] = ()
    reason: str = ""
    queue: tuple[int, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {"next": self.next, "deferred": list(self.deferred), "reason": self.reason, "queue": list(self.queue)}


def _landable(pull) -> bool:
    return getattr(pull, "checks", "") == "passing" and not getattr(pull, "draft", False)


def landing_order(pulls) -> list:
    """Oldest ready pull request first, then by number."""
    return sorted((p for p in pulls if _landable(p)), key=lambda p: (p.created_at, p.number))


def plan_update_branch(pulls, *, allow: bool = True) -> UpdatePlan:
    """At most one update: the first behind pull request in landing order."""
    order = landing_order(pulls)
    behind = [p.number for p in order if str(getattr(p, "merge_state", "") or "").upper() == BEHIND]
    if not behind:
        return UpdatePlan(None, (), "nothing behind", tuple(p.number for p in order))
    if not allow:
        return UpdatePlan(None, tuple(behind), "budget: branch updates are paused", tuple(p.number for p in order))
    if order[0].number != behind[0]:
        # The next to land is current. Updating anything behind it now is
        # thrown away when it merges.
        return UpdatePlan(
            None, tuple(behind), f"#{order[0].number} lands first; updates wait", tuple(p.number for p in order)
        )
    head, rest = behind[0], tuple(behind[1:])
    return UpdatePlan(head, rest, f"update #{head} only; {len(rest)} wait for it to land", tuple(p.number for p in order))
