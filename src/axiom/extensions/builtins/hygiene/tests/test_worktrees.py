# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for TIDY's stale-worktree assessor."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from axiom.extensions.builtins.hygiene import worktrees
from axiom.extensions.builtins.hygiene._git_isolation import (
    assert_test_tmp_path,
    git_isolated_env,
)


def _git(repo: Path, *args: str) -> None:
    assert_test_tmp_path(repo)
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env=git_isolated_env(),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "test@example.com")
    _git(r, "config", "user.name", "Test")
    (r / "README.md").write_text("seed\n")
    _git(r, "add", "README.md")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _add_worktree(repo: Path, branch: str, dir_name: str) -> Path:
    target = repo.parent / dir_name
    _git(repo, "worktree", "add", "-b", branch, str(target))
    return target


def test_list_worktrees_includes_main_and_extras(repo):
    _add_worktree(repo, "feat/foo", "wt-foo")
    wts = worktrees.list_worktrees(repo)
    branches = {wt.branch for wt in wts}
    assert "main" in branches
    assert "feat/foo" in branches


def test_find_stale_excludes_main_worktree(repo):
    verdicts = worktrees.find_stale(repo)
    assert verdicts == []


def test_s1_missing_directory_is_stale(repo):
    wt_path = _add_worktree(repo, "feat/gone", "wt-gone")
    # Nuke the directory but leave git's tracking entry
    import shutil

    shutil.rmtree(wt_path)
    verdicts = worktrees.find_stale(repo)
    [v] = [v for v in verdicts if v.worktree.branch == "feat/gone"]
    assert v.is_stale
    assert any("S1" in r for r in v.reasons)


def test_s2_branch_deleted_on_origin_is_stale(repo):
    _add_worktree(repo, "feat/upstream-gone", "wt-up")

    with (
        patch.object(worktrees, "_branch_exists_on_origin", return_value=False),
        patch.object(worktrees, "_is_ancestor_of_default", return_value=False),
        patch.object(worktrees, "_pr_state_for_branch", return_value=None),
    ):
        verdicts = worktrees.find_stale(repo)

    [v] = [v for v in verdicts if v.worktree.branch == "feat/upstream-gone"]
    assert v.is_stale
    assert any("S2" in r for r in v.reasons)


def test_s3_ancestor_of_main_is_stale(repo):
    _add_worktree(repo, "feat/already-merged", "wt-merged")

    with (
        patch.object(worktrees, "_branch_exists_on_origin", return_value=True),
        patch.object(worktrees, "_is_ancestor_of_default", return_value=True),
        patch.object(worktrees, "_pr_state_for_branch", return_value=None),
    ):
        verdicts = worktrees.find_stale(repo)

    [v] = [v for v in verdicts if v.worktree.branch == "feat/already-merged"]
    assert v.is_stale
    assert any("S3" in r for r in v.reasons)


def test_s4_merged_pr_is_stale(repo):
    _add_worktree(repo, "feat/squash-merged", "wt-squash")

    with (
        patch.object(worktrees, "_branch_exists_on_origin", return_value=True),
        patch.object(worktrees, "_is_ancestor_of_default", return_value=False),
        patch.object(worktrees, "_pr_state_for_branch", return_value="MERGED"),
    ):
        verdicts = worktrees.find_stale(repo)

    [v] = [v for v in verdicts if v.worktree.branch == "feat/squash-merged"]
    assert v.is_stale
    assert any("S4" in r and "MERGED" in r for r in v.reasons)


def test_no_signals_means_not_stale(repo):
    _add_worktree(repo, "feat/active", "wt-active")

    with (
        patch.object(worktrees, "_branch_exists_on_origin", return_value=True),
        patch.object(worktrees, "_is_ancestor_of_default", return_value=False),
        patch.object(worktrees, "_pr_state_for_branch", return_value="OPEN"),
    ):
        verdicts = worktrees.find_stale(repo)

    [v] = [v for v in verdicts if v.worktree.branch == "feat/active"]
    assert not v.is_stale
    assert v.reasons == []


def test_dirty_with_real_changes_blocks_force_prune(repo):
    wt = _add_worktree(repo, "feat/dirty", "wt-dirty")
    (wt / "real_change.py").write_text("# significant work\n")

    with (
        patch.object(worktrees, "_branch_exists_on_origin", return_value=False),
        patch.object(worktrees, "_is_ancestor_of_default", return_value=False),
        patch.object(worktrees, "_pr_state_for_branch", return_value=None),
    ):
        verdicts = worktrees.find_stale(repo)

    [v] = [v for v in verdicts if v.worktree.branch == "feat/dirty"]
    assert v.is_stale  # signals fired
    assert v.is_dirty
    assert v.can_force_prune is False  # safety floor engaged


def test_dirty_with_only_nits_still_force_prunable(repo):
    wt = _add_worktree(repo, "feat/nits", "wt-nits")
    (wt / "__pycache__").mkdir()
    (wt / "__pycache__" / "x.pyc").write_text("\x00")
    (wt / ".DS_Store").write_text("")

    with (
        patch.object(worktrees, "_branch_exists_on_origin", return_value=False),
        patch.object(worktrees, "_is_ancestor_of_default", return_value=False),
        patch.object(worktrees, "_pr_state_for_branch", return_value=None),
    ):
        verdicts = worktrees.find_stale(repo)

    [v] = [v for v in verdicts if v.worktree.branch == "feat/nits"]
    assert v.is_stale
    assert v.is_dirty
    assert v.can_force_prune is True  # nits don't block prune


def test_cli_emits_json_on_request(repo, capsys):
    _add_worktree(repo, "feat/json-shape", "wt-json")
    from axiom.extensions.builtins.hygiene import cli

    with (
        patch.object(worktrees, "_branch_exists_on_origin", return_value=False),
        patch.object(worktrees, "_is_ancestor_of_default", return_value=False),
        patch.object(worktrees, "_pr_state_for_branch", return_value=None),
    ):
        rc = cli.main(["--json", "list", "worktrees", "--repo", str(repo)])

    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    rows = [r for r in payload if r["branch"] == "feat/json-shape"]
    assert rows
    assert rows[0]["is_stale"] is True
    assert any("S2" in r for r in rows[0]["reasons"])


def test_cli_dry_run_does_not_remove_worktree(repo, capsys):
    wt_path = _add_worktree(repo, "feat/dry", "wt-dry")
    from axiom.extensions.builtins.hygiene import cli

    with (
        patch.object(worktrees, "_branch_exists_on_origin", return_value=False),
        patch.object(worktrees, "_is_ancestor_of_default", return_value=False),
        patch.object(worktrees, "_pr_state_for_branch", return_value=None),
    ):
        rc = cli.main(["list", "worktrees", "--repo", str(repo), "--dry-run"])

    assert rc == 0
    assert wt_path.exists()  # still there


def test_skill_doc_is_present():
    skill = (
        Path(__file__).parents[1]
        / "agents"
        / "tidy"
        / "skills"
        / "worktree-hygiene.md"
    )
    text = skill.read_text(encoding="utf-8")
    assert "# SKILL: worktree-hygiene" in text
    for marker in ("S1", "S2", "S3", "S4"):
        assert marker in text, f"signal {marker} must be documented"


def test_manifest_registers_worktree_skill():
    import tomllib

    manifest = (
        Path(__file__).parents[1] / "axiom-extension.toml"
    )
    data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    skills = [
        p for p in data["extension"]["provides"] if p.get("kind") == "skill"
    ]
    names = {s.get("name") for s in skills}
    assert "worktree-hygiene" in names
