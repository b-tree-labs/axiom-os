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

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


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


def _git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None


def branch_landed(branch: str, *, repo: Path) -> bool | None:
    """Has `branch` merged into its repo's trunk? RIVET's call, not TIDY's.

    This is the ``branch_landed`` TIDY injects into ``lane_reclaim.assess``.
    Deciding landed-ness is RIVET's authority (ADR-046); a lane may be clean
    by every other measure and still be live, so the answer that reclamation
    hangs on is the one answer TIDY is not allowed to compute for itself.

    Conservative by construction, because the cost is asymmetric. ``True``
    only when the branch tip is strictly an ancestor of the trunk — the
    definition of merged. ``False`` when the branch exists and is not yet an
    ancestor. ``None`` — *cannot tell* — for everything else: no repo, an
    unresolvable branch, no discoverable trunk, a git that errors. ``assess``
    treats ``None`` as a block, which is the point: unsure never reclaims.
    """
    if not branch or repo is None or not Path(repo).exists():
        return None
    # The trunk this branch would land on. Prefer the remote's idea of it
    # (what CI merges into), fall back to a local default branch.
    trunk = None
    head = _git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], cwd=repo)
    if head and head.returncode == 0 and head.stdout.strip():
        trunk = head.stdout.strip()  # e.g. "origin/main"
    else:
        for cand in ("origin/main", "origin/master", "main", "master"):
            probe = _git(["rev-parse", "--verify", "--quiet", cand], cwd=repo)
            if probe and probe.returncode == 0:
                trunk = cand
                break
    if trunk is None:
        return None
    exists = _git(["rev-parse", "--verify", "--quiet", branch], cwd=repo)
    if not exists or exists.returncode != 0:
        return None
    ancestor = _git(["merge-base", "--is-ancestor", branch, trunk], cwd=repo)
    if ancestor is None:
        return None
    # 0 = ancestor (landed), 1 = not an ancestor, anything else = could not tell.
    return True if ancestor.returncode == 0 else (False if ancestor.returncode == 1 else None)


def landed_probe(lanes: dict) -> Callable[[str], bool | None]:
    """A ``branch_landed(branch)`` closure bound to where each branch lives.

    ``assess`` calls ``branch_landed(branch)`` with the branch alone, but the
    merge question only has an answer inside a repository. RIVET already owns
    the branch→lane map (:func:`lane_for_branch`), so it owns resolving the
    branch to the checkout its lane recorded and asking git there. A branch
    whose lane has no on-disk root resolves to ``None`` — cannot tell — rather
    than guessing against some other repo.
    """
    roots: dict[str, Path] = {}
    for lane in lanes.values():
        b = getattr(lane, "branch", "")
        root = getattr(lane, "root", "")
        if b and root:
            roots.setdefault(b, Path(root))

    def probe(branch: str) -> bool | None:
        repo = roots.get(branch)
        if repo is None or not repo.exists():
            return None
        return branch_landed(branch, repo=repo)

    return probe


__all__ = ["LaneRef", "annotate", "branch_landed", "lane_for_branch", "landed_probe"]
