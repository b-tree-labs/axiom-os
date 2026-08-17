# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the category-level git cruft checks (2026-07 audit).

The audit found every ancestor-based merged-branch detector blind to
squash merges, no check at all for local branches whose upstream was
deleted (a deleted remote was hiding 1,877 unlanded lines), and the
primary checkout's untracked files reported nowhere. Worse, the
existing ``git_signals.check_*`` functions were unit-tested but DEAD
AT RUNTIME — no aggregator or CLI verb ever called them (the ADR-046
detect-only anti-pattern). This file covers:

  - ``content_landed`` — the patch-equivalence primitive
    (``git merge-tree --write-tree``; catches squash merges).
  - ``check_stale_branches`` / ``branch_prune.list_merged_local``
    flagging squash-landed branches.
  - ``check_upstream_gone_branches`` — landed → prune candidate;
    NOT landed → stranded-content warning.
  - ``check_primary_untracked`` — aged untracked files in the PRIMARY
    worktree (the one ``drift.gather_drift`` deliberately skips).
  - ``audit_git`` + the ``axi hygiene stat git`` verb, end-to-end —
    the wiring test whose absence let the checks die silently.

Fixture isolation per `feedback_test_fixture_isolation_required.md`.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from axiom.extensions.builtins.hygiene._git_isolation import (
    assert_test_tmp_path,
    git_isolated_env,
)
from axiom.extensions.builtins.hygiene.node_health import Finding, Severity


def _git(repo: Path, *args: str) -> str:
    assert_test_tmp_path(repo)
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=git_isolated_env(),
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with an `origin` remote that has `main` at one commit."""
    upstream = tmp_path / "upstream.git"
    upstream.mkdir()
    _git(upstream, "init", "-q", "--bare", "-b", "main")

    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "test@example.com")
    _git(r, "config", "user.name", "Test")
    _git(r, "remote", "add", "origin", str(upstream))
    (r / "README.md").write_text("seed\n")
    _git(r, "add", "README.md")
    _git(r, "commit", "-q", "-m", "init")
    _git(r, "push", "-q", "-u", "origin", "main")
    return r


def _branch_with_commits(repo: Path, name: str, n: int = 1) -> str:
    """Create `name` off main with `n` commits; return tip sha. Leaves the
    repo checked out on main."""
    _git(repo, "checkout", "-q", "-b", name, "main")
    for i in range(n):
        fname = name.replace("/", "_") + f"-{i}.txt"
        (repo / fname).write_text(f"{name} {i}\n")
        _git(repo, "add", fname)
        _git(repo, "commit", "-q", "-m", f"{name} commit {i}")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return sha


def _squash_land(repo: Path, name: str) -> None:
    """Squash-merge `name` into main (single new commit, no merge link)
    and push. The branch tip is NOT an ancestor of main afterwards —
    exactly the state that blinds ancestor-based detection."""
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--squash", "-q", name)
    _git(repo, "commit", "-q", "-m", f"squash-land {name}")
    _git(repo, "push", "-q", "origin", "main")


def _upstream_gone_branch(repo: Path, name: str, *, n: int = 1) -> str:
    """Create `name` with commits, push with upstream tracking, delete the
    remote ref, fetch --prune. Leaves `%(upstream:track)` == '[gone]'."""
    sha = _branch_with_commits(repo, name, n=n)
    _git(repo, "push", "-q", "-u", "origin", name)
    _git(repo, "push", "-q", "origin", "--delete", name)
    _git(repo, "fetch", "-q", "--prune", "origin")
    return sha


# ---------------------------------------------------------------------------
# content_landed — the patch-equivalence primitive
# ---------------------------------------------------------------------------


class TestContentLanded:
    def test_true_for_squash_landed_branch(self, repo):
        """A squash-merged branch's content IS on main even though its
        tip is not an ancestor — merge-tree yields main's own tree."""
        from axiom.extensions.builtins.hygiene.git_signals import content_landed

        _branch_with_commits(repo, "feat/squashed", n=2)
        _squash_land(repo, "feat/squashed")

        assert content_landed(repo, "feat/squashed", "main") is True

    def test_true_for_plain_ancestor_branch(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import content_landed

        _git(repo, "branch", "feat/at-main", "main")
        assert content_landed(repo, "feat/at-main", "main") is True

    def test_false_for_unlanded_branch(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import content_landed

        _branch_with_commits(repo, "feat/unlanded", n=1)
        assert content_landed(repo, "feat/unlanded", "main") is False

    def test_false_for_conflicting_branch(self, repo):
        """Branch and main both edited README differently — the virtual
        merge conflicts, so the content has definitely not landed."""
        from axiom.extensions.builtins.hygiene.git_signals import content_landed

        _git(repo, "checkout", "-q", "-b", "feat/conflict", "main")
        (repo / "README.md").write_text("branch version\n")
        _git(repo, "commit", "-q", "-am", "branch edit")
        _git(repo, "checkout", "-q", "main")
        (repo / "README.md").write_text("main version\n")
        _git(repo, "commit", "-q", "-am", "main edit")

        assert content_landed(repo, "feat/conflict", "main") is False

    def test_fallback_patch_id_single_commit(self, repo, monkeypatch):
        """Without merge-tree (git < 2.38) a single-commit squash-landed
        branch is still caught via patch-id (`git cherry`)."""
        from axiom.extensions.builtins.hygiene import git_signals as gs

        _branch_with_commits(repo, "feat/one", n=1)
        _squash_land(repo, "feat/one")
        monkeypatch.setattr(gs, "_merge_tree_supported", lambda: False)

        assert gs.content_landed(repo, "feat/one", "main") is True

    def test_fallback_multi_commit_returns_false(self, repo, monkeypatch):
        """Patch-id equivalence can't prove a multi-commit squash; the
        fallback stays conservative and reports not-landed."""
        from axiom.extensions.builtins.hygiene import git_signals as gs

        _branch_with_commits(repo, "feat/many", n=2)
        _squash_land(repo, "feat/many")
        monkeypatch.setattr(gs, "_merge_tree_supported", lambda: False)

        assert gs.content_landed(repo, "feat/many", "main") is False


# ---------------------------------------------------------------------------
# Squash-landed branches flagged + prunable
# ---------------------------------------------------------------------------


class TestSquashLandedDetection:
    def test_check_stale_branches_flags_squash_landed(self, repo):
        """The audit's headline gap: ancestor-based detection misses
        squash merges, so squash-landed branches linger forever."""
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_stale_branches,
        )

        _branch_with_commits(repo, "feat/squash-stale", n=2)
        _squash_land(repo, "feat/squash-stale")

        findings = check_stale_branches(repo)
        match = next(
            f for f in findings if f.current_value == "feat/squash-stale"
        )
        assert match.check == "stale_branch"
        assert match.auto_fixable is True

    def test_list_merged_local_includes_squash_landed(self, repo):
        from axiom.extensions.builtins.hygiene.branch_prune import (
            list_merged_local,
        )

        _branch_with_commits(repo, "feat/squash-prune", n=2)
        _squash_land(repo, "feat/squash-prune")

        names = {b for b, _ in list_merged_local(repo)}
        assert "feat/squash-prune" in names

    def test_prune_deletes_squash_landed_via_archive(self, repo, tmp_path):
        """The existing archive-then-delete path handles the squash-landed
        candidate: archived under refs/tidy-archive/, then deleted."""
        from axiom.extensions.builtins.hygiene.branch_prune import prune

        sha = _branch_with_commits(repo, "feat/squash-gone", n=1)
        _squash_land(repo, "feat/squash-gone")

        result = prune(repo, state_dir=tmp_path / "state", remote=False)

        assert result.proceed is True
        assert "feat/squash-gone" in result.pruned
        archived = _git(
            repo, "rev-parse", "refs/tidy-archive/local/feat/squash-gone"
        )
        assert archived == sha


# ---------------------------------------------------------------------------
# Upstream-gone branches
# ---------------------------------------------------------------------------


class TestCheckUpstreamGoneBranches:
    def test_no_findings_on_clean_repo(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_upstream_gone_branches,
        )

        assert check_upstream_gone_branches(repo) == []

    def test_landed_branch_is_prune_candidate(self, repo):
        """Upstream gone + content already on main → safe prune candidate."""
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_upstream_gone_branches,
        )

        _upstream_gone_branch(repo, "feat/landed-gone", n=1)
        _squash_land(repo, "feat/landed-gone")

        findings = check_upstream_gone_branches(repo)
        match = next(
            f for f in findings if f.current_value == "feat/landed-gone"
        )
        assert match.check == "upstream_gone_branch"
        assert match.severity == Severity.INFO
        assert match.auto_fixable is True

    def test_unlanded_branch_is_stranded_content_warning(self, repo):
        """Upstream gone + content NOT on main is the dangerous case —
        today a deleted remote was hiding 1,877 unlanded lines. Never a
        prune candidate; the human must land or archive it first."""
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_upstream_gone_branches,
        )

        _upstream_gone_branch(repo, "feat/stranded", n=2)

        findings = check_upstream_gone_branches(repo)
        match = next(f for f in findings if f.current_value == "feat/stranded")
        assert match.check == "upstream_gone_branch"
        assert match.severity == Severity.WARNING
        assert match.auto_fixable is False
        assert "stranded" in match.message.lower()

    def test_branch_without_upstream_not_flagged(self, repo):
        """Local-only WIP (never pushed) is not 'upstream gone'."""
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_upstream_gone_branches,
        )

        _branch_with_commits(repo, "feat/local-only", n=1)

        findings = check_upstream_gone_branches(repo)
        names = [f.current_value for f in findings]
        assert "feat/local-only" not in names

    def test_current_branch_not_flagged(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_upstream_gone_branches,
        )

        _upstream_gone_branch(repo, "feat/gone-current", n=1)
        _git(repo, "checkout", "-q", "feat/gone-current")

        findings = check_upstream_gone_branches(repo)
        names = [f.current_value for f in findings]
        assert "feat/gone-current" not in names

    def test_worktree_occupied_branch_not_flagged(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_upstream_gone_branches,
        )

        _upstream_gone_branch(repo, "feat/gone-occupied", n=1)
        wt_path = repo.parent / "wt-gone-occupied"
        _git(repo, "worktree", "add", "-q", str(wt_path), "feat/gone-occupied")

        findings = check_upstream_gone_branches(repo)
        names = [f.current_value for f in findings]
        assert "feat/gone-occupied" not in names

    def test_finding_shape(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_upstream_gone_branches,
        )

        _upstream_gone_branch(repo, "feat/gone-shape", n=1)
        [match] = check_upstream_gone_branches(repo)
        assert isinstance(match, Finding)
        assert match.current_value == "feat/gone-shape"


# ---------------------------------------------------------------------------
# Primary-checkout untracked files
# ---------------------------------------------------------------------------


def _age_file(path: Path, days: float) -> None:
    """Back-date a file's mtime by `days`."""
    ts = time.time() - days * 86400.0
    os.utime(path, (ts, ts))


class TestCheckPrimaryUntracked:
    def test_clean_primary_no_findings(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_primary_untracked,
        )

        assert check_primary_untracked(repo) == []

    def test_old_untracked_file_flagged(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_primary_untracked,
        )

        stray = repo / "forgotten-notes.txt"
        stray.write_text("wip\n")
        _age_file(stray, days=10)

        [match] = check_primary_untracked(repo, min_age_days=3)
        assert match.check == "primary_untracked"
        assert "forgotten-notes.txt" in match.message
        assert match.auto_fixable is False

    def test_fresh_untracked_file_not_flagged(self, repo):
        """A file created just now is probably active work — don't nag."""
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_primary_untracked,
        )

        (repo / "active-work.txt").write_text("fresh\n")

        assert check_primary_untracked(repo, min_age_days=3) == []

    def test_ignored_file_not_flagged(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_primary_untracked,
        )

        (repo / ".gitignore").write_text("*.log\n")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-q", "-m", "ignore logs")
        junk = repo / "debug.log"
        junk.write_text("noise\n")
        _age_file(junk, days=10)

        findings = check_primary_untracked(repo, min_age_days=3)
        assert not any("debug.log" in f.message for f in findings)

    def test_one_finding_lists_all_old_files(self, repo):
        """Report-only, one Finding for the whole batch — a per-file
        finding flood would drown the rest of the audit."""
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_primary_untracked,
        )

        for name in ("stray-a.txt", "stray-b.txt"):
            p = repo / name
            p.write_text("x\n")
            _age_file(p, days=10)

        [match] = check_primary_untracked(repo, min_age_days=3)
        assert "stray-a.txt" in match.message
        assert "stray-b.txt" in match.message

    def test_covers_primary_even_when_secondary_worktrees_exist(self, repo):
        """drift.gather_drift excludes the main worktree; this check is
        the surface that deliberately covers it."""
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_primary_untracked,
        )

        wt_path = repo.parent / "wt-secondary"
        _git(repo, "worktree", "add", "-q", "-b", "feat/sec", str(wt_path))
        stray = repo / "primary-only.txt"
        stray.write_text("in the primary checkout\n")
        _age_file(stray, days=10)

        [match] = check_primary_untracked(repo, min_age_days=3)
        assert "primary-only.txt" in match.message


# ---------------------------------------------------------------------------
# audit_git — the aggregator that makes the checks LIVE
# ---------------------------------------------------------------------------


class TestAuditGit:
    def test_empty_repo_yields_no_findings(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import audit_git

        assert audit_git(repo) == []

    def test_runs_the_previously_orphaned_checks(self, repo):
        """audit_git must surface stale branches AND dormant stashes AND
        upstream-gone branches in one call — the checks the audit found
        dead at runtime."""
        from axiom.extensions.builtins.hygiene.git_signals import audit_git

        # stale branch (tip == origin/main)
        _git(repo, "branch", "feat/stale-agg", "main")
        # dormant stash (dormancy is 7d; back-dating stash commits is
        # awkward, so assert on the stale branch + upstream-gone rows and
        # separately that the stash check is in the fan-out below)
        _upstream_gone_branch(repo, "feat/agg-stranded", n=1)

        findings = audit_git(repo)
        checks = {f.check for f in findings}
        assert "stale_branch" in checks
        assert "upstream_gone_branch" in checks

    def test_one_broken_check_does_not_sink_the_audit(self, repo, monkeypatch):
        from axiom.extensions.builtins.hygiene import git_signals as gs

        def boom(_repo):
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(gs, "check_dormant_stashes", boom)
        _git(repo, "branch", "feat/still-found", "main")

        findings = gs.audit_git(repo)
        assert any(f.current_value == "feat/still-found" for f in findings)

    def test_fan_out_includes_every_check(self, repo):
        """Guard against the next silently-orphaned check: every public
        check_* in git_signals must be wired into audit_git."""
        import inspect

        from axiom.extensions.builtins.hygiene import git_signals as gs

        expected = {
            name
            for name, obj in inspect.getmembers(gs, inspect.isfunction)
            if name.startswith("check_") and obj.__module__ == gs.__name__
        }
        wired = {fn.__name__ for fn in gs.AUDIT_GIT_CHECKS}
        assert wired == expected


# ---------------------------------------------------------------------------
# CLI wiring — `axi hygiene stat git` end-to-end
# (the audit found the existing checks died precisely because no wiring
# test existed; this is that test)
# ---------------------------------------------------------------------------


def _subprocess_env(tmp_path) -> dict:
    """Env for a CLI subprocess that imports the SAME source as the
    in-process tests (mirrors test_branch_prune._subprocess_env)."""
    from axiom.extensions.builtins.hygiene import git_signals as gs

    src_root = Path(gs.__file__).resolve().parents[4]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src_root) + os.pathsep + env.get("PYTHONPATH", "")
    env["AXI_STATE_DIR"] = str(tmp_path / "state")
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    return env


class TestStatGitVerbWiring:
    def test_stat_git_surfaces_findings_end_to_end(self, repo, tmp_path):
        import sys

        _git(repo, "branch", "feat/wire-stale", "main")
        env = _subprocess_env(tmp_path)
        proc = subprocess.run(
            [sys.executable, "-m",
             "axiom.extensions.builtins.hygiene.cli",
             "stat", "git", "--repo", str(repo)],
            capture_output=True, text=True, env=env,
        )
        assert proc.returncode == 0, proc.stderr
        assert "feat/wire-stale" in proc.stdout
        assert "stale_branch" in proc.stdout

    def test_stat_git_json(self, repo, tmp_path):
        import json as _json
        import sys

        _git(repo, "branch", "feat/wire-json", "main")
        env = _subprocess_env(tmp_path)
        proc = subprocess.run(
            [sys.executable, "-m",
             "axiom.extensions.builtins.hygiene.cli",
             "stat", "git", "--repo", str(repo), "--json"],
            capture_output=True, text=True, env=env,
        )
        assert proc.returncode == 0, proc.stderr
        payload = _json.loads(proc.stdout)
        assert any(
            f["check"] == "stale_branch"
            and f["current_value"] == "feat/wire-json"
            for f in payload["findings"]
        )

    def test_stat_git_clean_repo_reports_clean(self, repo, tmp_path):
        import sys

        env = _subprocess_env(tmp_path)
        proc = subprocess.run(
            [sys.executable, "-m",
             "axiom.extensions.builtins.hygiene.cli",
             "stat", "git", "--repo", str(repo)],
            capture_output=True, text=True, env=env,
        )
        assert proc.returncode == 0, proc.stderr
        assert "clean" in proc.stdout.lower()

    def test_stat_git_registered_as_skill_resource(self):
        """`hygiene.stat` must accept the `git` resource (ADR-056: the
        verb is a thin wrapper over the registered skill)."""
        from axiom.extensions.builtins.hygiene.skills import _STAT_RESOURCES

        assert "git" in _STAT_RESOURCES
