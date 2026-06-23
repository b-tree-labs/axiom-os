# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for TIDY's branch/remote-ref prune executor (ADR-046).

TIDY owns destructive git working-state cleanup. `check_stale_branches`
(git_signals) only *detects*; this module *executes* — closing the
detect-only gap — under the ADR-045 D6 contract:

  - reversibility: every prune archives the ref under `refs/tidy-archive/`
    before deleting, so the action is undoable (D6.2).
  - guarded: runs through `agent_action_guard.guarded_act` at tier N with
    `volume_mode="confirm"`, so an over-baseline batch downgrades to a
    confirmation prompt rather than acting blindly (D6.3).
  - confirmation: a branch is a candidate only when merged into the
    default branch (`git branch [-r] --merged`); protected branches and
    in-use/current branches are never touched.

Fixture isolation per `feedback_test_fixture_isolation_required.md`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from axiom.extensions.builtins.hygiene._git_isolation import (
    assert_test_tmp_path,
    git_isolated_env,
)


def _git(repo: Path, *args: str) -> str:
    assert_test_tmp_path(repo)
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True,
        capture_output=True, text=True, env=git_isolated_env(),
    )
    return result.stdout.strip()


def _git_rc(repo: Path, *args: str) -> int:
    assert_test_tmp_path(repo)
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True,
        text=True, env=git_isolated_env(),
    ).returncode


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A clone with `origin` (bare) holding `main`."""
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


def _merged_branch(repo: Path, name: str, *, push: bool = False) -> str:
    """Create `name`, add a commit, merge it into main (no-ff), return its
    pre-merge tip sha. The branch is then merged-by-content into main.
    Optionally push the branch to origin first (so a remote ref exists)."""
    fname = name.replace("/", "_") + ".txt"
    _git(repo, "checkout", "-q", "-b", name)
    (repo / fname).write_text("x\n")
    _git(repo, "add", fname)
    _git(repo, "commit", "-q", "-m", f"work on {name}")
    sha = _git(repo, "rev-parse", "HEAD")
    if push:
        _git(repo, "push", "-q", "-u", "origin", name)
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", f"merge {name}", name)
    _git(repo, "push", "-q", "origin", "main")
    return sha


def _unmerged_branch(repo: Path, name: str) -> str:
    fname = name.replace("/", "_") + ".txt"
    _git(repo, "checkout", "-q", "-b", name)
    (repo / fname).write_text("y\n")
    _git(repo, "add", fname)
    _git(repo, "commit", "-q", "-m", f"unmerged {name}")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return sha


# ---------------------------------------------------------------------------
# Candidate listing
# ---------------------------------------------------------------------------


class TestListMergedLocal:
    def test_lists_merged_not_unmerged(self, repo):
        from axiom.extensions.builtins.hygiene.branch_prune import (
            list_merged_local,
        )
        _merged_branch(repo, "feat/done")
        _unmerged_branch(repo, "feat/wip")
        names = {b for b, _ in list_merged_local(repo)}
        assert "feat/done" in names
        assert "feat/wip" not in names
        assert "main" not in names  # protected/current

    def test_excludes_current_branch(self, repo):
        from axiom.extensions.builtins.hygiene.branch_prune import (
            list_merged_local,
        )
        _merged_branch(repo, "feat/done")
        _git(repo, "checkout", "-q", "feat/done")  # now current, though merged
        names = {b for b, _ in list_merged_local(repo)}
        assert "feat/done" not in names


class TestListMergedRemote:
    def test_lists_merged_remote_branch(self, repo):
        from axiom.extensions.builtins.hygiene.branch_prune import (
            list_merged_remote,
        )
        _merged_branch(repo, "feat/shipped", push=True)
        _git(repo, "fetch", "-q", "origin")
        names = {b for b, _ in list_merged_remote(repo)}
        assert "feat/shipped" in names
        assert "main" not in names


# ---------------------------------------------------------------------------
# Reversible prune (local)
# ---------------------------------------------------------------------------


class TestPruneLocalReversible:
    def test_prune_archives_then_deletes(self, repo, tmp_path):
        from axiom.extensions.builtins.hygiene.branch_prune import prune
        sha = _merged_branch(repo, "feat/done")

        result = prune(repo, state_dir=tmp_path / "state", remote=False)

        assert result.proceed is True
        assert "feat/done" in result.pruned
        # branch ref gone
        assert _git_rc(repo, "rev-parse", "--verify", "-q",
                       "refs/heads/feat/done") != 0
        # archive ref exists, pointing at the pre-delete tip
        archived = _git(repo, "rev-parse",
                        "refs/tidy-archive/local/feat/done")
        assert archived == sha

    def test_undo_restores_branch(self, repo, tmp_path):
        from axiom.extensions.builtins.hygiene.branch_prune import prune, undo
        sha = _merged_branch(repo, "feat/done")
        prune(repo, state_dir=tmp_path / "state", remote=False)

        undo(repo, "feat/done", remote=False)

        restored = _git(repo, "rev-parse", "refs/heads/feat/done")
        assert restored == sha

    def test_dry_run_does_not_delete(self, repo, tmp_path):
        from axiom.extensions.builtins.hygiene.branch_prune import prune
        _merged_branch(repo, "feat/done")
        result = prune(repo, state_dir=tmp_path / "state",
                       remote=False, dry_run=True)
        assert result.reason == "dry_run"
        assert _git_rc(repo, "rev-parse", "--verify", "-q",
                       "refs/heads/feat/done") == 0  # still there


# ---------------------------------------------------------------------------
# Reversible prune (remote)
# ---------------------------------------------------------------------------


class TestPruneRemoteReversible:
    def test_prune_remote_deletes_origin_ref_and_archives(self, repo, tmp_path):
        from axiom.extensions.builtins.hygiene.branch_prune import prune
        sha = _merged_branch(repo, "feat/shipped", push=True)
        _git(repo, "fetch", "-q", "origin")

        result = prune(repo, state_dir=tmp_path / "state", remote=True)

        assert result.proceed is True
        assert "feat/shipped" in result.pruned
        # remote ref gone
        assert "feat/shipped" not in _git(repo, "ls-remote", "--heads",
                                          "origin")
        # archived locally for undo
        archived = _git(repo, "rev-parse",
                        "refs/tidy-archive/remote/feat/shipped")
        assert archived == sha


# ---------------------------------------------------------------------------
# Guard integration (ADR-045 D6)
# ---------------------------------------------------------------------------


class TestGuardIntegration:
    def test_over_limit_batch_downgrades_to_confirmation(self, repo, tmp_path,
                                                         monkeypatch):
        """A batch over the per-tick limit downgrades to needs_confirmation
        (D6.3) and deletes nothing until confirmed."""
        from axiom.extensions.builtins.hygiene.branch_prune import prune
        monkeypatch.setenv("TIDY_GIT_BRANCH_DELETE_MAX_PER_TICK", "2")
        for i in range(4):
            _merged_branch(repo, f"feat/done{i}")

        result = prune(repo, state_dir=tmp_path / "state", remote=False)

        assert result.proceed is False
        assert result.reason.startswith("needs_confirmation")
        # nothing deleted
        for i in range(4):
            assert _git_rc(repo, "rev-parse", "--verify", "-q",
                           f"refs/heads/feat/done{i}") == 0

    def test_confirmed_over_limit_batch_proceeds(self, repo, tmp_path,
                                                 monkeypatch):
        from axiom.extensions.builtins.hygiene.branch_prune import prune
        monkeypatch.setenv("TIDY_GIT_BRANCH_DELETE_MAX_PER_TICK", "2")
        for i in range(4):
            _merged_branch(repo, f"feat/done{i}")

        result = prune(repo, state_dir=tmp_path / "state", remote=False,
                       confirmed=True)

        assert result.proceed is True
        assert len(result.pruned) == 4


# ---------------------------------------------------------------------------
# CLI subprocess smoke (feedback_cli_subprocess_smoke_required)
# ---------------------------------------------------------------------------


def _subprocess_env(tmp_path) -> dict:
    """Env for a CLI subprocess that imports the SAME source as the in-process
    tests. The child interpreter doesn't inherit pytest's sys.path insertion,
    so point PYTHONPATH at the src root of the branch_prune module under test
    (otherwise it would import the editable-installed package instead)."""
    import os
    from pathlib import Path

    from axiom.extensions.builtins.hygiene import branch_prune as bp

    # .../src/axiom/extensions/builtins/hygiene/branch_prune.py → parents[4] = src
    src_root = Path(bp.__file__).resolve().parents[4]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src_root) + os.pathsep + env.get("PYTHONPATH", "")
    env["AXI_STATE_DIR"] = str(tmp_path / "state")
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    return env


class TestRemoteDeleteRouting:
    """Remote delete must not hang on a credential prompt (the 2026-05-26
    cleanup hit this: `git push origin --delete` blocked on osxkeychain).
    For GitHub remotes, route through `gh api` (token already scoped); for
    others, fall back to `git push --delete` with prompts disabled."""

    import pytest

    @pytest.mark.parametrize("url,slug", [
        ("https://github.com/b-tree-labs/axiom-os.git", "b-tree-labs/axiom-os"),
        ("git@github.com:owner/repo.git", "owner/repo"),
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://gitlab.example.org/org/example-repo.git", None),
        ("", None),
    ])
    def test_github_slug_detection(self, url, slug):
        from axiom.extensions.builtins.hygiene.branch_prune import _github_slug
        assert _github_slug(url) == slug

    def test_github_remote_uses_gh_api_not_git_push(self, repo, tmp_path,
                                                    monkeypatch):
        from axiom.extensions.builtins.hygiene import branch_prune as bp
        sha = _merged_branch(repo, "feat/done", push=True)
        _git(repo, "fetch", "-q", "origin")
        calls: list = []
        # Pretend origin is a GitHub remote; capture the gh-api delete.
        monkeypatch.setattr(bp, "_github_slug", lambda url: "owner/repo")
        monkeypatch.setattr(bp, "_gh_delete_ref",
                            lambda slug, branch: calls.append((slug, branch)) or True)

        ok = bp._prune_one_remote(repo, "feat/done", sha, "origin")

        assert ok is True
        assert calls == [("owner/repo", "feat/done")]
        # archive written first (reversible), regardless of delete path
        assert _git(repo, "rev-parse",
                    "refs/tidy-archive/remote/feat/done") == sha

    def test_safe_git_env_disables_terminal_prompt(self):
        """No axiom git subprocess should ever hang waiting on a credential
        prompt — it must fail fast instead."""
        from axiom.infra.git import safe_git_env
        assert safe_git_env().get("GIT_TERMINAL_PROMPT") == "0"


class TestCliSmoke:
    def test_branches_list_via_subprocess(self, repo, tmp_path):
        import subprocess
        import sys

        _merged_branch(repo, "feat/done")
        env = _subprocess_env(tmp_path)
        proc = subprocess.run(
            [sys.executable, "-m",
             "axiom.extensions.builtins.hygiene.cli",
             "list", "branches", "--repo", str(repo)],
            capture_output=True, text=True, env=env,
        )
        assert proc.returncode == 0, proc.stderr
        assert "Merged local branches" in proc.stdout
        assert "feat/done" in proc.stdout

    def test_branches_prune_via_subprocess(self, repo, tmp_path):
        import subprocess
        import sys

        _merged_branch(repo, "feat/done")
        env = _subprocess_env(tmp_path)
        proc = subprocess.run(
            [sys.executable, "-m",
             "axiom.extensions.builtins.hygiene.cli",
             "list", "branches", "--repo", str(repo), "--prune"],
            capture_output=True, text=True, env=env,
        )
        assert proc.returncode == 0, proc.stderr
        assert "Pruned 1" in proc.stdout
        assert _git_rc(repo, "rev-parse", "--verify", "-q",
                       "refs/heads/feat/done") != 0
