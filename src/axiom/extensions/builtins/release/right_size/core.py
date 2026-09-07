# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Core recommendation logic — provider-agnostic.

Inputs:
  - the proposed change (branch name, files touched, intent)
  - in-flight PRs from the same author (via a RepoProvider)
Outputs:
  - one Recommendation per call (open / fold / stack / wait), with a
    rationale + CI-cost estimate + per-PR overlap detail.

The function is pure: same inputs → same recommendation. Side-effects
(persistence, MCP serialization, CLI rendering) live in the adapter
layer that calls this.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from axiom.extensions.builtins.release.right_size.providers import (
    InFlightPR,
    PRDiff,
    RepoProvider,
)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


RecommendationKind = Literal[
    "open_new", "fold_into", "stack_on", "wait_for"
]
SizeBucket = Literal["tiny", "small", "medium", "large"]


@dataclass(frozen=True)
class ProposedChange:
    """What the caller wants to open as a PR."""

    branch_name: str
    files: tuple[str, ...]
    intent: str
    """One-line description of what this PR is for; used in rationale."""
    additions: int = 0
    """Lines added; cheap to compute via `git diff --shortstat`."""
    deletions: int = 0


@dataclass(frozen=True)
class Recommendation:
    """One recommendation. ``target_pr`` is set for fold / stack / wait."""

    kind: RecommendationKind
    rationale: str
    target_pr: int | None = None
    target_pr_url: str | None = None
    cost_estimate_minutes: int = 0
    """Estimated CI minutes saved vs opening a standalone PR."""
    overlap: dict[int, tuple[str, ...]] = field(default_factory=dict)
    """{pr_number: (overlapping_files...)}."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# Rough size buckets — informs the fold-vs-stack decision.
def _bucket(change: ProposedChange) -> SizeBucket:
    n_files = len(change.files)
    n_lines = change.additions + change.deletions
    if n_files <= 3 and n_lines <= 50:
        return "tiny"
    if n_files <= 10 and n_lines <= 250:
        return "small"
    if n_files <= 30 and n_lines <= 1000:
        return "medium"
    return "large"


def _overlap_with(
    change: ProposedChange, diff: PRDiff
) -> tuple[str, ...]:
    """Files this change shares with an in-flight PR's diff."""
    return tuple(sorted(set(change.files) & set(diff.files)))


def _ci_minutes_for(change: ProposedChange) -> int:
    """Rough cost of a standalone PR's CI. Tunable per repo later."""
    # Per the current matrix: lint + preflight + 3 Python versions +
    # install-path + build-wheel ≈ 12 min wall-time. Aggregate matrix
    # minutes (GH bills per job × runner) ≈ 30. Tunable per repo.
    base = int(os.environ.get("RIGHT_SIZE_CI_BASE_MIN", "12"))
    return base


@dataclass
class RightSizeContext:
    """Per-call context. Pass the provider + repo + author at call time.

    Tests can pass a stubbed provider and a frozen ``now``.
    """

    provider: RepoProvider
    repo: str
    author: str | None = None
    now: datetime | None = None
    stale_after_days: int = 14
    """If an open PR hasn't been touched in this many days, prefer
    standalone over wait — assume it's stuck or abandoned."""


# ---------------------------------------------------------------------------
# The recommendation engine
# ---------------------------------------------------------------------------


def recommend(
    change: ProposedChange,
    ctx: RightSizeContext,
) -> Recommendation:
    """Return the single best recommendation for this change.

    Decision priority (high → low):

    1. **wait_for** — at least one in-flight PR overlaps and is older
       than the staleness threshold or merging soon (head ready, not
       draft). Open this change after that PR merges to avoid rebases.
    2. **fold_into** — at least one open PR from this author overlaps
       AND this change is "tiny" AND the target PR is non-draft. Push
       to the target's branch instead.
    3. **stack_on** — an open PR is a logical base for this change
       (overlap exists; this branch is on top of theirs); recommend
       opening with the target PR's head as base.
    4. **open_new** — no overlap, no relevant in-flight PR; standalone
       is the right call.
    """
    now = ctx.now or datetime.now(timezone.utc)
    in_flight = ctx.provider.list_in_flight_prs(ctx.repo, ctx.author)
    bucket = _bucket(change)

    # Build overlap map + recency.
    overlaps: dict[int, tuple[str, ...]] = {}
    pr_by_number: dict[int, InFlightPR] = {p.number: p for p in in_flight}
    for pr in in_flight:
        diff = ctx.provider.diff_for_pr(ctx.repo, pr.number)
        if diff is None:
            continue
        common = _overlap_with(change, diff)
        if common:
            overlaps[pr.number] = common

    if not overlaps:
        return Recommendation(
            kind="open_new",
            rationale="no overlap with any in-flight PR; standalone is correct",
            cost_estimate_minutes=0,
        )

    stale_cutoff = now - timedelta(days=ctx.stale_after_days)

    # Rank candidates by overlap size; richer scoring follows in v2.
    ranked = sorted(
        overlaps.items(),
        key=lambda kv: (-len(kv[1]), kv[0]),
    )
    best_pr_num, best_overlap = ranked[0]
    target = pr_by_number[best_pr_num]

    # Rule 1: target is fresh + not draft + heavy overlap → wait?
    is_stale = target.updated_at < stale_cutoff
    has_heavy_overlap = len(best_overlap) >= max(2, len(change.files) // 2)
    if (
        has_heavy_overlap
        and not target.is_draft
        and not is_stale
    ):
        return Recommendation(
            kind="wait_for",
            rationale=(
                f"PR #{target.number} ({target.title!r}) overlaps "
                f"{len(best_overlap)}/{len(change.files)} files and is "
                "ready-for-review; wait for it to merge to avoid rebase "
                "churn and duplicate CI"
            ),
            target_pr=target.number,
            target_pr_url=target.url,
            cost_estimate_minutes=_ci_minutes_for(change),
            overlap=overlaps,
        )

    # Rule 2: tiny change + non-draft overlapping PR → fold.
    if bucket == "tiny" and not target.is_draft and not is_stale:
        return Recommendation(
            kind="fold_into",
            rationale=(
                f"change is tiny ({len(change.files)} files / "
                f"{change.additions + change.deletions} lines) and "
                f"shares {len(best_overlap)} file(s) with PR "
                f"#{target.number}; push to {target.head_branch} instead "
                "of opening a standalone PR"
            ),
            target_pr=target.number,
            target_pr_url=target.url,
            cost_estimate_minutes=_ci_minutes_for(change),
            overlap=overlaps,
        )

    # Rule 3: overlap exists but change is larger → stack.
    return Recommendation(
        kind="stack_on",
        rationale=(
            f"overlap with PR #{target.number} ({len(best_overlap)} "
            f"file(s)); open this PR with {target.head_branch} as the "
            "base so CI tests the stack incrementally"
        ),
        target_pr=target.number,
        target_pr_url=target.url,
        cost_estimate_minutes=_ci_minutes_for(change) // 2,
        overlap=overlaps,
    )


__all__ = [
    "ProposedChange",
    "Recommendation",
    "RecommendationKind",
    "RightSizeContext",
    "SizeBucket",
    "recommend",
]
