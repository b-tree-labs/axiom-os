# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for hygiene/drift.py — TIDY's drift dashboard.

The drift module surfaces the *industrial common case* TIDY's stale-detection
explicitly refuses to act on: branches that are alive but drifting, with no
PR opened, slowly accumulating merge debt.

The module is read-only by design — it produces decision packets a human reads
before choosing PR / pause / archive. No automated action.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from axiom.extensions.builtins.hygiene._git_isolation import (
    assert_test_tmp_path,
    git_isolated_env,
)


# ----- Test fixture: real-but-tiny git repo ---------------------------------


def _run(args: list[str], cwd: Path) -> str:
    assert_test_tmp_path(cwd)
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, check=True,
        env=git_isolated_env(),
    ).stdout


def _commit(repo: Path, name: str, body: str = "x", msg: str = "msg") -> str:
    (repo / name).write_text(body)
    _run(["git", "add", name], repo)
    _run(["git", "commit", "-m", msg], repo)
    return _run(["git", "rev-parse", "HEAD"], repo).strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A tiny repo with origin/main set up via a bare upstream + initial commit."""

    upstream = tmp_path / "upstream.git"
    work = tmp_path / "work"
    upstream.mkdir()
    work.mkdir()
    _run(["git", "init", "--bare", "-b", "main"], upstream)
    _run(["git", "init", "-b", "main"], work)
    _run(["git", "config", "user.email", "t@example.com"], work)
    _run(["git", "config", "user.name", "T"], work)
    _run(["git", "remote", "add", "origin", str(upstream)], work)
    _commit(work, "README", "init", "init repo")
    _run(["git", "push", "-u", "origin", "main"], work)
    return work


def _make_branch_with_commits(
    repo: Path, branch: str, n_commits: int, msg_prefix: str = "feat"
) -> Path:
    """Create a worktree off origin/main with n new commits."""

    wt_path = repo.parent / f"wt-{branch.replace('/', '-')}"
    _run(["git", "worktree", "add", "-b", branch, str(wt_path), "origin/main"], repo)
    for i in range(n_commits):
        _commit(wt_path, f"f{i}.txt", body=f"v{i}", msg=f"{msg_prefix}: step {i}")
    return wt_path


# ----- gather_drift: shape + counts -----------------------------------------


def test_gather_drift_empty_repo_returns_no_extras(repo):
    """Only the main worktree exists → drift report is empty."""
    from axiom.extensions.builtins.hygiene.drift import gather_drift

    reports = gather_drift(repo)
    assert reports == []


def test_gather_drift_branch_ahead_records_ahead_count(repo):
    """A worktree branch with 3 fresh commits records ahead=3, behind=0, dirty=0."""
    from axiom.extensions.builtins.hygiene.drift import gather_drift

    _make_branch_with_commits(repo, "feat/foo", n_commits=3)
    reports = gather_drift(repo)
    assert len(reports) == 1
    r = reports[0]
    assert r.branch == "feat/foo"
    assert r.ahead == 3
    assert r.behind == 0
    assert r.dirty_files == 0


def test_gather_drift_counts_dirty_files(repo):
    """Uncommitted modifications surface in dirty_files."""
    from axiom.extensions.builtins.hygiene.drift import gather_drift

    wt = _make_branch_with_commits(repo, "feat/dirty", n_commits=1)
    (wt / "extra.txt").write_text("untracked")
    (wt / "f0.txt").write_text("modified")
    reports = gather_drift(repo)
    [r] = reports
    assert r.dirty_files >= 2


# ----- Severity classification ----------------------------------------------


def test_severity_fresh_for_recent_branch(repo):
    """ahead=2 on a branch one second old → severity 'fresh'."""
    from axiom.extensions.builtins.hygiene.drift import gather_drift

    _make_branch_with_commits(repo, "feat/fresh", n_commits=2)
    [r] = gather_drift(repo)
    assert r.drift_severity == "fresh"


def test_severity_buckets_documented():
    """Severity buckets are explicit + ordered fresh < moderate < stale < ancient."""
    from axiom.extensions.builtins.hygiene.drift import SEVERITY_ORDER

    assert SEVERITY_ORDER == ("fresh", "moderate", "stale", "ancient")


# ----- Suggested action heuristics ------------------------------------------


def test_suggested_action_fresh_with_no_pr_is_continue(repo):
    """A fresh branch with no PR yet should be 'continue' (not yet PR-worthy)."""
    from axiom.extensions.builtins.hygiene.drift import gather_drift

    _make_branch_with_commits(repo, "feat/just-started", n_commits=1)
    [r] = gather_drift(repo)
    assert r.suggested_action == "continue"


def test_suggested_action_dormant_branch_proposes_pr_or_pause(monkeypatch, repo):
    """ahead > 0, no PR, behind ≥ MODERATE_BEHIND → 'open-pr-or-pause'.

    We simulate behind > 0 by classifying via fake git_age + behind values.
    """
    from axiom.extensions.builtins.hygiene import drift as drift_mod
    from axiom.extensions.builtins.hygiene.drift import (
        WorktreeDrift,
        suggest_action,
    )

    r = WorktreeDrift(
        path=repo / "wt-x",
        branch="feat/dormant",
        ahead=5,
        behind=40,
        unpushed=5,
        dirty_files=0,
        last_commit_age_days=10,
        has_open_pr=False,
        pr_state=None,
        purpose=drift_mod.BranchPurpose(
            branch_name="feat/dormant",
            inferred_topic="dormant work",
            related_adrs=[],
            related_prds=[],
        ),
        recent_commit_subjects=[],
        top_changed_paths=[],
        drift_severity="stale",
        suggested_action="",
        decision_packet="",
    )
    assert suggest_action(r) == "open-pr-or-pause"


def test_suggested_action_with_open_pr_is_monitor(repo):
    """When a PR is open, no further action is proposed — TIDY stands down."""
    from axiom.extensions.builtins.hygiene.drift import (
        BranchPurpose,
        WorktreeDrift,
        suggest_action,
    )

    r = WorktreeDrift(
        path=repo / "wt-y",
        branch="feat/has-pr",
        ahead=5,
        behind=10,
        unpushed=0,
        dirty_files=0,
        last_commit_age_days=2,
        has_open_pr=True,
        pr_state="OPEN",
        purpose=BranchPurpose("feat/has-pr", "with PR", [], []),
        recent_commit_subjects=[],
        top_changed_paths=[],
        drift_severity="moderate",
        suggested_action="",
        decision_packet="",
    )
    assert suggest_action(r) == "monitor-pr"


def test_suggested_action_pr_merged_is_archive(repo):
    """A merged PR → branch should be archived/removed."""
    from axiom.extensions.builtins.hygiene.drift import (
        BranchPurpose,
        WorktreeDrift,
        suggest_action,
    )

    r = WorktreeDrift(
        path=repo / "wt-z",
        branch="feat/merged",
        ahead=0,
        behind=10,
        unpushed=0,
        dirty_files=0,
        last_commit_age_days=5,
        has_open_pr=False,
        pr_state="MERGED",
        purpose=BranchPurpose("feat/merged", "merged", [], []),
        recent_commit_subjects=[],
        top_changed_paths=[],
        drift_severity="moderate",
        suggested_action="",
        decision_packet="",
    )
    assert suggest_action(r) == "archive"


# ----- Branch purpose inference ---------------------------------------------


def test_purpose_extracts_adr_references_from_commit_messages(repo):
    """Commit subjects mentioning ADR-NNN are surfaced in purpose.related_adrs."""
    from axiom.extensions.builtins.hygiene.drift import infer_purpose

    wt = _make_branch_with_commits(repo, "feat/adr-touch", n_commits=0)
    _commit(wt, "x", "x", "feat: implement ADR-019 routing")
    _commit(wt, "y", "y", "fix: align with ADR-019 §4.2")

    purpose = infer_purpose(repo_path=repo, worktree_path=wt, branch="feat/adr-touch")
    assert "ADR-019" in purpose.related_adrs


def test_purpose_extracts_prd_slugs(repo):
    """Commit subjects mentioning prd-<slug> are surfaced in purpose.related_prds."""
    from axiom.extensions.builtins.hygiene.drift import infer_purpose

    wt = _make_branch_with_commits(repo, "feat/prd-touch", n_commits=0)
    _commit(wt, "x", "x", "wip: per prd-classroom plan")

    purpose = infer_purpose(repo_path=repo, worktree_path=wt, branch="feat/prd-touch")
    assert "prd-classroom" in purpose.related_prds


def test_purpose_topic_falls_back_to_branch_name_segments(repo):
    """When commits don't reveal a topic, derive from the branch slug."""
    from axiom.extensions.builtins.hygiene.drift import infer_purpose

    wt = _make_branch_with_commits(repo, "design/scientific-displays", n_commits=1)
    purpose = infer_purpose(
        repo_path=repo, worktree_path=wt, branch="design/scientific-displays"
    )
    assert "scientific" in purpose.inferred_topic.lower()


# ----- Decision packet ------------------------------------------------------


def test_decision_packet_contains_state_snapshot(repo):
    """The packet text contains ahead/behind/dirty so HITL can act on it."""
    from axiom.extensions.builtins.hygiene.drift import gather_drift

    _make_branch_with_commits(repo, "feat/packet-1", n_commits=2)
    [r] = gather_drift(repo)
    pkt = r.decision_packet
    assert "feat/packet-1" in pkt
    assert "ahead=2" in pkt or "ahead: 2" in pkt
    # At least one of the three signal words must appear
    assert any(word in pkt.lower() for word in ("dirty", "behind", "ahead"))


def test_decision_packet_lists_recent_commit_subjects(repo):
    """The packet shows recent commit subjects so the human can recall what's there."""
    from axiom.extensions.builtins.hygiene.drift import gather_drift

    wt = _make_branch_with_commits(repo, "feat/packet-2", n_commits=0)
    _commit(wt, "x", "x", "feat: distinct-subject-token-XYZ")
    [r] = gather_drift(repo)
    assert "distinct-subject-token-XYZ" in r.decision_packet


def test_decision_packet_includes_suggested_action(repo):
    """The packet ends with the suggested next action so the human knows the proposal."""
    from axiom.extensions.builtins.hygiene.drift import gather_drift

    _make_branch_with_commits(repo, "feat/packet-3", n_commits=2)
    [r] = gather_drift(repo)
    assert r.suggested_action in r.decision_packet


# ----- Suspicious-author detection (R3) -------------------------------------


def test_suspicious_identity_detection_catches_tester():
    """The 2026-05-04 incident's exact author/email pair must trigger."""
    from axiom.extensions.builtins.hygiene.drift import _is_suspicious_identity

    assert _is_suspicious_identity("tester", "t@t.test")
    assert _is_suspicious_identity("Tester", "anything@example.com")
    assert _is_suspicious_identity("pytest-runner", "pytest@somewhere")
    assert not _is_suspicious_identity("Benjamin Booth", "ben@b-treeventures.com")
    assert not _is_suspicious_identity("Alice", "alice@gmail.com")


def _make_poisoned_branch(repo: Path, name: str, msg: str = "init") -> Path:
    wt = _make_branch_with_commits(repo, name, n_commits=0)
    _run(["git", "config", "--local", "user.name", "tester"], wt)
    _run(["git", "config", "--local", "user.email", "t@t.test"], wt)
    _commit(wt, "x.txt", "x", msg)
    _run(["git", "config", "--local", "user.name", "Benjamin Booth"], wt)
    _run(["git", "config", "--local", "user.email", "ben@b-treeventures.com"], wt)
    return wt


def test_gather_drift_flags_branch_with_tester_commits(repo):
    """A branch whose commit is authored 'tester' surfaces in suspicious_commits."""
    from axiom.extensions.builtins.hygiene.drift import gather_drift

    _make_poisoned_branch(repo, "feat/poisoned")
    [r] = gather_drift(repo)
    assert len(r.suspicious_commits) == 1
    assert r.suspicious_commits[0].author_name == "tester"


def test_suspicious_branch_action_is_quarantine(repo):
    """A branch with suspicious commits gets quarantine-suspicious-authors regardless of drift."""
    from axiom.extensions.builtins.hygiene.drift import gather_drift

    _make_poisoned_branch(repo, "feat/poisoned-2")
    [r] = gather_drift(repo)
    assert r.suggested_action == "quarantine-suspicious-authors"


def test_decision_packet_lists_suspicious_commits(repo):
    """The packet shows offending SHAs + authors so the human can rewrite history."""
    from axiom.extensions.builtins.hygiene.drift import gather_drift

    _make_poisoned_branch(repo, "feat/poisoned-3", msg="init-distinct-token-XYZ")
    [r] = gather_drift(repo)
    assert "SUSPICIOUS COMMITS" in r.decision_packet
    assert "tester" in r.decision_packet
    assert "init-distinct-token-XYZ" in r.decision_packet
