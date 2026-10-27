# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Deterministic safety floors for TIDY's worktree prune.

These guard against the failure that forced a manual override: `tidy worktrees
--prune` proposing to remove a worktree another session holds (locked) or a base
checkout on the default branch. The floors sit *under* TIDY's staleness/LLM
judgment — they constrain what may be auto-removed, they don't decide what's
stale.
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.hygiene import worktrees
from axiom.extensions.builtins.hygiene.worktrees import (
    StalenessVerdict,
    WorktreeInfo,
    plan_prune,
)


def _verdict(
    path: str,
    *,
    branch: str = "feat/x",
    stale: bool = True,
    locked: bool = False,
    dirty: bool = False,
    can_force: bool = True,
) -> StalenessVerdict:
    wt = WorktreeInfo(path=Path(path), branch=branch, head_sha="abc123", locked=locked)
    return StalenessVerdict(
        worktree=wt,
        is_stale=stale,
        is_dirty=dirty,
        can_force_prune=can_force,
        reasons=["S3: ancestor of main"] if stale else [],
    )


# --- default-branch guard -------------------------------------------------

def test_default_branch_worktree_is_not_stale(tmp_path):
    # A linked worktree checked out on the default branch is a base checkout,
    # not stale feature work — it must never be flagged (even though S3 would
    # trivially fire: its HEAD is an ancestor of main).
    wt = WorktreeInfo(path=tmp_path, branch="main", head_sha="deadbeef")
    verdict = worktrees.assess_staleness(wt, tmp_path, default_branch="main")
    assert verdict.is_stale is False
    assert any("default branch" in r for r in verdict.reasons)


# --- plan_prune floors ----------------------------------------------------

def test_locked_worktree_skipped_by_default():
    plan = plan_prune([_verdict("/wt/locked", locked=True)])
    assert plan.to_prune == []
    assert len(plan.skipped) == 1
    assert "locked" in plan.skipped[0][1]


def test_locked_worktree_reclaimed_only_when_named_in_only():
    v = _verdict("/wt/locked", locked=True)
    plan = plan_prune([v], only=["/wt/locked"])
    assert [x.worktree.path for x in plan.to_prune] == [Path("/wt/locked")]
    assert plan.skipped == []


def test_blanket_force_does_not_override_a_lock():
    # --force is for dirty trees (git semantics), not for stealing a lock.
    plan = plan_prune([_verdict("/wt/locked", locked=True)], force=True)
    assert plan.to_prune == []
    assert "locked" in plan.skipped[0][1]


def test_only_restricts_to_named_paths():
    plan = plan_prune(
        [_verdict("/wt/a"), _verdict("/wt/b"), _verdict("/wt/c")],
        only=["/wt/b"],
    )
    assert [x.worktree.path for x in plan.to_prune] == [Path("/wt/b")]
    assert sorted(str(v.worktree.path) for v, _ in plan.skipped) == ["/wt/a", "/wt/c"]


def test_exclude_removes_named_paths():
    plan = plan_prune([_verdict("/wt/a"), _verdict("/wt/b")], exclude=["/wt/a"])
    assert [x.worktree.path for x in plan.to_prune] == [Path("/wt/b")]
    assert plan.skipped[0][0].worktree.path == Path("/wt/a")
    assert "exclude" in plan.skipped[0][1]


def test_dirty_skipped_unless_force():
    v = _verdict("/wt/dirty", dirty=True, can_force=False)
    assert plan_prune([v]).to_prune == []
    assert plan_prune([v], force=True).to_prune == [v]


def test_non_stale_never_in_plan():
    plan = plan_prune([_verdict("/wt/clean", stale=False)])
    assert plan.to_prune == []
    assert plan.skipped == []  # not a candidate at all
