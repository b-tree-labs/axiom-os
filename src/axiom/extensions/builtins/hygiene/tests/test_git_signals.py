# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for repo-wide git-state hygiene signals (issue #201).

Per the issue: the hygiene agent must proactively surface stale
branches / orphan worktrees / dormant stashes / scaffolds-that-never-
graduated, instead of waiting for a human audit. Each signal is a pure
function over the repo's file-tree + git state.

This file tests the FIRST signal — `check_stale_branches` — which
flags local branches whose tip is reachable from `origin/main` and
which are NOT currently checked out in any worktree (i.e. merged + not
in use = candidate for deletion).

Fixture isolation per `feedback_test_fixture_isolation_required.md`:
every git invocation uses `git_isolated_env()` + paths under
`tmp_path`.
"""

from __future__ import annotations

import subprocess
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


class TestCheckStaleBranches:
    def test_returns_empty_list_when_only_main_exists(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_stale_branches,
        )
        assert check_stale_branches(repo) == []

    def test_flags_branch_whose_tip_is_on_origin_main(self, repo):
        """A branch pointing at the same SHA as `origin/main` is merged
        by definition — flag for deletion."""
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_stale_branches,
        )
        # Create branch pointing at the same commit as main.
        _git(repo, "branch", "feat/merged-already", "main")
        # Switch off main so we're not "currently checked out" on the
        # branch we'd flag.
        _git(repo, "checkout", "-q", "-b", "work")

        findings = check_stale_branches(repo)
        names = [f.current_value for f in findings]
        assert "feat/merged-already" in names

    def test_does_not_flag_branch_with_unmerged_commits(self, repo):
        """A branch with its own commit beyond origin/main is NOT stale."""
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_stale_branches,
        )
        _git(repo, "checkout", "-q", "-b", "feat/wip")
        (repo / "wip.txt").write_text("not on main\n")
        _git(repo, "add", "wip.txt")
        _git(repo, "commit", "-q", "-m", "wip")

        findings = check_stale_branches(repo)
        names = [f.current_value for f in findings]
        assert "feat/wip" not in names

    def test_does_not_flag_currently_checked_out_branch(self, repo):
        """Even if the current branch is merged, don't flag — the
        user is actively on it."""
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_stale_branches,
        )
        _git(repo, "checkout", "-q", "-b", "feat/on-it")
        # No new commit — tip == origin/main → would normally be stale.
        findings = check_stale_branches(repo)
        names = [f.current_value for f in findings]
        assert "feat/on-it" not in names

    def test_does_not_flag_main(self, repo):
        """`main` is the protected branch; never flag."""
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_stale_branches,
        )
        findings = check_stale_branches(repo)
        names = [f.current_value for f in findings]
        assert "main" not in names

    def test_does_not_flag_branch_checked_out_in_other_worktree(self, repo):
        """A branch currently in a worktree is in use — don't flag,
        even if it's merged."""
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_stale_branches,
        )
        # Create a worktree on a new branch (which by construction has
        # tip == origin/main since we haven't committed there yet).
        wt_path = repo.parent / "wt-occupied"
        _git(repo, "worktree", "add", "-q", "-b", "feat/occupied", str(wt_path))

        findings = check_stale_branches(repo)
        names = [f.current_value for f in findings]
        assert "feat/occupied" not in names

    def test_finding_shape(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_stale_branches,
        )
        _git(repo, "branch", "feat/safe-delete", "main")
        _git(repo, "checkout", "-q", "-b", "work")

        findings = check_stale_branches(repo)
        match = next(f for f in findings if f.current_value == "feat/safe-delete")
        assert isinstance(match, Finding)
        assert match.check == "stale_branch"
        assert match.severity == Severity.INFO
        assert match.auto_fixable is True
        assert "feat/safe-delete" in match.message
        assert "merged" in match.message.lower()

    def test_multiple_stale_branches_all_flagged(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_stale_branches,
        )
        for name in ("feat/a", "feat/b", "feat/c"):
            _git(repo, "branch", name, "main")
        _git(repo, "checkout", "-q", "-b", "work")

        findings = check_stale_branches(repo)
        names = {f.current_value for f in findings}
        assert names >= {"feat/a", "feat/b", "feat/c"}


class TestCheckOrphanWorktrees:
    """Orphan worktrees: prunable per git, OR on a branch that's already
    on origin/main, OR on a branch deleted on origin. Wraps the
    existing `worktrees.find_stale` verdicts into Findings."""

    def test_no_worktrees_no_findings(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_orphan_worktrees,
        )
        assert check_orphan_worktrees(repo) == []

    def test_prunable_worktree_is_flagged(self, repo):
        """Worktree dir deleted on disk → git marks prunable → flag."""
        import shutil
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_orphan_worktrees,
        )

        wt_path = repo.parent / "wt-gone"
        _git(repo, "worktree", "add", "-q", "-b", "feat/gone", str(wt_path))
        shutil.rmtree(wt_path)

        findings = check_orphan_worktrees(repo)
        paths = [f.current_value for f in findings]
        assert any("wt-gone" in p for p in paths)

    def test_worktree_on_merged_branch_is_flagged(self, repo):
        """Worktree at a SHA that's ancestor of origin/main → stale (S3)."""
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_orphan_worktrees,
        )

        wt_path = repo.parent / "wt-merged"
        # Worktree on a fresh branch == tip of main == ancestor of main
        _git(repo, "worktree", "add", "-q", "-b", "feat/merged-wt", str(wt_path))

        findings = check_orphan_worktrees(repo)
        paths = [f.current_value for f in findings]
        assert any("wt-merged" in p for p in paths)

    def test_active_worktree_with_unmerged_commit_not_flagged(self, repo):
        """Worktree with its own commit beyond origin/main → not stale."""
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_orphan_worktrees,
        )

        wt_path = repo.parent / "wt-active"
        _git(repo, "worktree", "add", "-q", "-b", "feat/active-wt", str(wt_path))
        (wt_path / "ahead.txt").write_text("ahead of main\n")
        _git(wt_path, "add", "ahead.txt")
        _git(wt_path, "commit", "-q", "-m", "ahead")

        findings = check_orphan_worktrees(repo)
        paths = [f.current_value for f in findings]
        assert not any("wt-active" in p for p in paths)

    def test_finding_shape(self, repo):
        import shutil
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_orphan_worktrees,
        )
        from axiom.extensions.builtins.hygiene.node_health import (
            Finding,
            Severity,
        )

        wt_path = repo.parent / "wt-shape"
        _git(repo, "worktree", "add", "-q", "-b", "feat/shape", str(wt_path))
        shutil.rmtree(wt_path)

        findings = check_orphan_worktrees(repo)
        match = next(f for f in findings if "wt-shape" in f.current_value)
        assert isinstance(match, Finding)
        assert match.check == "orphan_worktree"
        assert match.severity in (Severity.INFO, Severity.WARNING)
        assert match.auto_fixable is True


class TestGitDirEnvIsolation:
    """Regression: when `git_signals._run` doesn't strip `GIT_DIR` from
    inherited env, every check leaks to the parent process's git repo.
    The pre-push hook context is the trigger — git sets `GIT_DIR`
    before invoking hook subprocesses; that env propagates into pytest,
    then into every `subprocess.run(["git", ...])` the tests make.

    With `cwd=<tmp_path>` git would normally discover `.git` via cwd,
    but `GIT_DIR` short-circuits that and points at whatever the parent
    set. The 2026-05-19 pre-push run had `check_dup_basenames` "find
    duplicates" on a freshly-created tmp_path repo because `git
    ls-files` was actually running against the axiom repo (many
    duplicate basenames) instead of the empty tmp repo (zero).
    """

    def test_check_dup_basenames_isolated_from_polluting_GIT_DIR(
        self, repo, monkeypatch,
    ):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_dup_basenames,
        )

        # Build a polluting repo with tracked files that have duplicate
        # basenames — exactly the smell `check_dup_basenames` flags.
        polluting_dir = repo.parent / "polluter"
        polluting_dir.mkdir()
        _git(polluting_dir, "init", "-q", "-b", "main")
        _git(polluting_dir, "config", "user.email", "test@example.com")
        _git(polluting_dir, "config", "user.name", "Test")
        (polluting_dir / "a").mkdir()
        (polluting_dir / "b").mkdir()
        (polluting_dir / "a" / "config.toml").write_text("a")
        (polluting_dir / "b" / "config.toml").write_text("b")
        _git(polluting_dir, "add", "-A")
        _git(polluting_dir, "commit", "-q", "-m", "duplicate basenames")

        # Point GIT_DIR at the polluter. Without isolation, the test's
        # `git ls-files` (run via cwd=<tmp repo>) would see the
        # polluter's tracked files and surface a duplicate-basenames
        # finding for `config.toml`.
        monkeypatch.setenv("GIT_DIR", str(polluting_dir / ".git"))

        # The fixture's `repo` has only README.md — zero duplicates.
        findings = check_dup_basenames(repo)
        names = [f.current_value for f in findings]
        assert all("config.toml" not in n for n in names), (
            f"GIT_DIR leaked: check_dup_basenames flagged config.toml from "
            f"the polluting repo. Findings: {findings}"
        )

    def test_check_stale_branches_isolated_from_polluting_GIT_DIR(
        self, repo, monkeypatch,
    ):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_stale_branches,
        )

        polluting = repo.parent / "polluting2.git"
        polluting.mkdir()
        _git(polluting, "init", "-q", "--bare", "-b", "main")
        monkeypatch.setenv("GIT_DIR", str(polluting))

        # Whatever findings come back, every flagged branch must exist
        # in the fixture's tmp repo (not a different one).
        findings = check_stale_branches(repo)
        valid_branches = {"main"}
        for f in findings:
            assert f.current_value in valid_branches, (
                f"Got finding for branch {f.current_value!r} that isn't in "
                f"the fixture repo — GIT_DIR pollution leaked"
            )


def _stash_one(repo: Path, msg: str) -> None:
    """Helper: create a non-empty change + stash with message."""
    p = repo / f"stash-{msg}.tmp"
    p.write_text("wip\n")
    _git(repo, "stash", "push", "-q", "-u", "-m", msg)


class TestCheckDormantStashes:
    """Stashes older than the dormancy threshold are surface-level
    candidates for review / drop."""

    def test_no_stashes_no_findings(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_dormant_stashes,
        )
        assert check_dormant_stashes(repo) == []

    def test_recent_stash_not_flagged(self, repo):
        """A stash created right now is not dormant — don't flag."""
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_dormant_stashes,
        )
        _stash_one(repo, "fresh")

        findings = check_dormant_stashes(repo, dormancy_days=60)
        assert findings == []

    def test_old_stash_is_flagged(self, repo):
        """A stash older than the dormancy threshold IS flagged.

        The dormancy check uses the stash commit's author date. We
        set the threshold to 0 days so even a just-created stash
        trips the check — that exercises the date-extraction path
        without sleeping or back-dating commits."""
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_dormant_stashes,
        )
        _stash_one(repo, "old")

        findings = check_dormant_stashes(repo, dormancy_days=0)
        assert len(findings) >= 1
        assert any("old" in f.message for f in findings)

    def test_finding_shape(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_dormant_stashes,
        )
        from axiom.extensions.builtins.hygiene.node_health import (
            Finding,
            Severity,
        )
        _stash_one(repo, "shape")

        [match] = check_dormant_stashes(repo, dormancy_days=0)
        assert isinstance(match, Finding)
        assert match.check == "dormant_stash"
        assert match.severity == Severity.INFO
        # Stash deletion is destructive — never auto-fix.
        assert match.auto_fixable is False
        assert match.current_value.startswith("stash@{")


class TestCheckDupBasenames:
    """Same filename tracked at multiple paths is a smell — usually a
    leftover from a refactor. The 2026-05 cleanup surfaced
    `scripts/setup-<host>.sh` duplicated under multiple paths
    coexisting; this signal would have flagged that pair."""

    def test_no_duplicates_returns_empty(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_dup_basenames,
        )
        assert check_dup_basenames(repo) == []

    def test_same_basename_at_two_paths_flagged(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_dup_basenames,
        )
        (repo / "a").mkdir()
        (repo / "b").mkdir()
        (repo / "a" / "config.toml").write_text("a")
        (repo / "b" / "config.toml").write_text("b")
        _git(repo, "add", "a/config.toml", "b/config.toml")
        _git(repo, "commit", "-q", "-m", "dup")

        [match] = check_dup_basenames(repo)
        assert match.check == "duplicate_basename"
        assert "config.toml" in match.message
        assert "a/config.toml" in match.current_value
        assert "b/config.toml" in match.current_value

    def test_common_basenames_are_skipped(self, repo):
        """`__init__.py`, `README.md`, `tests`, etc. legitimately recur
        at many paths; don't flag them as duplicates."""
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_dup_basenames,
        )
        (repo / "pkg_a").mkdir()
        (repo / "pkg_b").mkdir()
        (repo / "pkg_a" / "__init__.py").write_text("")
        (repo / "pkg_b" / "__init__.py").write_text("")
        _git(repo, "add", "pkg_a/__init__.py", "pkg_b/__init__.py")
        _git(repo, "commit", "-q", "-m", "init files")

        findings = check_dup_basenames(repo)
        names = [f.message for f in findings]
        assert not any("__init__.py" in m for m in names)


class TestCheckSelfSimilarDirs:
    """`X/X/` directory tree is almost always a path-duplication
    accident (a real `infra/infra/` duplication was the motivating example)."""

    def test_normal_repo_returns_empty(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_self_similar_dirs,
        )
        assert check_self_similar_dirs(repo) == []

    def test_X_in_X_is_flagged(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_self_similar_dirs,
        )
        (repo / "infra" / "infra").mkdir(parents=True)
        (repo / "infra" / "infra" / "marker.txt").write_text("oops")
        _git(repo, "add", "infra/infra/marker.txt")
        _git(repo, "commit", "-q", "-m", "self-similar")

        [match] = check_self_similar_dirs(repo)
        assert match.check == "self_similar_directory"
        assert "infra/infra" in match.current_value

    def test_unrelated_nesting_not_flagged(self, repo):
        """`infra/db/` is fine — only same-name self-similarity flags."""
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_self_similar_dirs,
        )
        (repo / "infra" / "db").mkdir(parents=True)
        (repo / "infra" / "db" / "schema.sql").write_text("--")
        _git(repo, "add", "infra/db/schema.sql")
        _git(repo, "commit", "-q", "-m", "infra/db is fine")

        assert check_self_similar_dirs(repo) == []


class TestCheckHardcodedPathScripts:
    """`scripts/*.sh` containing absolute developer paths (`$HOME/Projects/...`,
    `/Users/.../Projects/...`) can't run anywhere else. The 2026-05
    `scripts/mo-heartbeat.sh` was the motivating failure case."""

    def test_clean_scripts_no_findings(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_scripts_with_hardcoded_paths,
        )
        (repo / "scripts").mkdir()
        (repo / "scripts" / "ok.sh").write_text(
            "#!/usr/bin/env bash\nNEUT_DIR=\"${REPO_DIR:-$(pwd)}\"\necho \"$NEUT_DIR\"\n"
        )
        _git(repo, "add", "scripts/ok.sh")
        _git(repo, "commit", "-q", "-m", "clean script")

        assert check_scripts_with_hardcoded_paths(repo) == []

    def test_home_projects_path_is_flagged(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_scripts_with_hardcoded_paths,
        )
        (repo / "scripts").mkdir()
        (repo / "scripts" / "bad.sh").write_text(
            '#!/usr/bin/env bash\nPROJ_DIR="$HOME/Projects/example-org/consumer"\n'
        )
        _git(repo, "add", "scripts/bad.sh")
        _git(repo, "commit", "-q", "-m", "bad script")

        [match] = check_scripts_with_hardcoded_paths(repo)
        assert match.check == "hardcoded_developer_path"
        assert "scripts/bad.sh" in match.current_value
        assert "$HOME/Projects" in match.message

    def test_users_path_is_flagged(self, repo):
        """`/Users/<name>/Projects/...` is the macOS equivalent and
        equally non-portable."""
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_scripts_with_hardcoded_paths,
        )
        (repo / "scripts").mkdir()
        (repo / "scripts" / "mac.sh").write_text(
            '#!/usr/bin/env bash\ncd /Users/example/Projects/something || exit 1\n'
        )
        _git(repo, "add", "scripts/mac.sh")
        _git(repo, "commit", "-q", "-m", "mac script")

        [match] = check_scripts_with_hardcoded_paths(repo)
        assert match.check == "hardcoded_developer_path"
        assert "mac.sh" in match.current_value

    def test_only_scans_scripts_dir(self, repo):
        """A `.sh` file elsewhere (e.g. `docs/example.sh`) isn't subject
        to this rule — only `scripts/*.sh` because that's the documented
        portable-scripts namespace."""
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_scripts_with_hardcoded_paths,
        )
        (repo / "docs").mkdir()
        (repo / "docs" / "example.sh").write_text(
            '#!/usr/bin/env bash\ncd $HOME/Projects/whatever\n'
        )
        _git(repo, "add", "docs/example.sh")
        _git(repo, "commit", "-q", "-m", "example outside scripts/")

        assert check_scripts_with_hardcoded_paths(repo) == []

    def test_finding_shape(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_scripts_with_hardcoded_paths,
        )
        from axiom.extensions.builtins.hygiene.node_health import (
            Finding,
            Severity,
        )
        (repo / "scripts").mkdir()
        (repo / "scripts" / "shape.sh").write_text(
            '#!/usr/bin/env bash\nROOT=$HOME/Projects/X\n'
        )
        _git(repo, "add", "scripts/shape.sh")
        _git(repo, "commit", "-q", "-m", "shape")

        [match] = check_scripts_with_hardcoded_paths(repo)
        assert isinstance(match, Finding)
        assert match.severity == Severity.WARNING
        assert match.auto_fixable is False  # path replacement is structural


class TestCheckNonGraduatedScaffolds:
    """Scaffolds tracked by `axi ext init` that haven't been marked
    graduated and are older than the dormancy threshold surface as
    Findings. Motivating case: a stranded `chat_agent/` prototype, sat for
    weeks with one source file."""

    def test_no_registry_no_findings(self, repo):
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_non_graduated_scaffolds,
        )
        assert check_non_graduated_scaffolds(repo) == []

    def test_recent_scaffold_not_flagged(self, repo):
        from axiom.cli.ext.scaffold_registry import record_scaffold
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_non_graduated_scaffolds,
        )
        (repo / "ext-foo").mkdir()
        record_scaffold(repo, name="ext-foo", ext_path=repo / "ext-foo")

        findings = check_non_graduated_scaffolds(repo, dormancy_days=14)
        assert findings == []

    def test_old_non_graduated_scaffold_flagged(self, repo):
        """Threshold=0 surfaces even a just-created scaffold; tests the
        date-comparison path without sleeping."""
        from axiom.cli.ext.scaffold_registry import record_scaffold
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_non_graduated_scaffolds,
        )
        (repo / "ext-old").mkdir()
        record_scaffold(repo, name="ext-old", ext_path=repo / "ext-old")

        [match] = check_non_graduated_scaffolds(repo, dormancy_days=0)
        assert match.check == "non_graduated_scaffold"
        assert "ext-old" in match.message

    def test_graduated_scaffold_not_flagged(self, repo):
        from axiom.cli.ext.scaffold_registry import (
            graduate_scaffold,
            record_scaffold,
        )
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_non_graduated_scaffolds,
        )
        (repo / "ext-done").mkdir()
        record_scaffold(repo, name="ext-done", ext_path=repo / "ext-done")
        graduate_scaffold(repo, name="ext-done")

        findings = check_non_graduated_scaffolds(repo, dormancy_days=0)
        assert findings == []

    def test_finding_shape(self, repo):
        from axiom.cli.ext.scaffold_registry import record_scaffold
        from axiom.extensions.builtins.hygiene.git_signals import (
            check_non_graduated_scaffolds,
        )
        from axiom.extensions.builtins.hygiene.node_health import (
            Finding,
            Severity,
        )
        (repo / "ext-shape").mkdir()
        record_scaffold(repo, name="ext-shape", ext_path=repo / "ext-shape")

        [match] = check_non_graduated_scaffolds(repo, dormancy_days=0)
        assert isinstance(match, Finding)
        assert match.severity == Severity.INFO
        # Graduation is a structural decision (does the ext have first
        # non-trivial test? does lint pass?), so not auto-fixable.
        assert match.auto_fixable is False
        assert match.current_value == "ext-shape"
