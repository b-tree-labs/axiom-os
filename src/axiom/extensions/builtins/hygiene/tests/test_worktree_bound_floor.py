# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""F2 — a worktree something is importing from must not be pruned.

Written against a removal that actually happened on 2026-09-28.
`appkit-wt-cause` was landed, merged, clean and prunable. Every staleness
signal this module has fired, and every one of them was correct. Two
long-running servers had been importing a package from it for two days;
nothing noticed until a stylesheet returned 500.

The lesson is narrow and worth keeping narrow: git-clean is not the same as
unused, and the binding that makes a worktree used lives in a virtualenv
where git cannot see it.
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.hygiene import worktrees


def _venv_with_editable(root: Path, name: str, package: str, target: Path) -> Path:
    site = root / name / "lib" / "python3.14" / "site-packages"
    site.mkdir(parents=True)
    (site / f"__editable__.{package}.pth").write_text(f"{target}\n")
    return root / name


def test_a_binding_is_found_across_the_venvs_we_check(tmp_path):
    wt = tmp_path / "appkit-wt-cause"
    (wt / "src").mkdir(parents=True)
    venv = _venv_with_editable(tmp_path, ".venv", "axiom_appkit", wt / "src")

    bound = worktrees.editable_bindings(wt, [venv])

    assert bound == [(".venv", "axiom_appkit")]


def test_a_worktree_nothing_points_at_is_unbound(tmp_path):
    wt = tmp_path / "quiet-worktree"
    wt.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    venv = _venv_with_editable(tmp_path, ".venv", "something", other)

    assert worktrees.editable_bindings(wt, [venv]) == []


def test_a_binding_to_a_SIBLING_is_not_a_binding_to_this_one(tmp_path):
    """The check is containment, not prefix-matching on a string —
    `appkit-wt-cause2` must not look like `appkit-wt-cause`."""
    wt = tmp_path / "appkit-wt-cause"
    wt.mkdir()
    sibling = tmp_path / "appkit-wt-cause2"
    sibling.mkdir()
    venv = _venv_with_editable(tmp_path, ".venv", "pkg", sibling)

    assert worktrees.editable_bindings(wt, [venv]) == []


def test_the_floor_blocks_force_prune_however_stale_the_signals(tmp_path, monkeypatch):
    """The whole point: S2, S3 and S4 all firing does not override F2."""
    wt_path = tmp_path / "appkit-wt-cause"
    wt_path.mkdir()
    venv = _venv_with_editable(tmp_path, ".venv", "axiom_appkit", wt_path)

    info = worktrees.WorktreeInfo(path=wt_path, branch="feat/landed", head_sha="abc123")
    monkeypatch.setattr(worktrees, "_dirty_status", lambda _p: (False, True))
    monkeypatch.setattr(worktrees, "_branch_exists_on_origin", lambda *_a: False)
    monkeypatch.setattr(worktrees, "_is_ancestor_of_default", lambda *_a: True)
    monkeypatch.setattr(worktrees, "_pr_state_for_branch", lambda *_a: "MERGED")

    verdict = worktrees.assess_staleness(info, tmp_path, default_branch="main", venvs=[venv])

    assert verdict.is_stale is True, "the git signals are correct and should still fire"
    assert verdict.is_dirty is False, "it really is clean"
    assert verdict.can_force_prune is False, "but something is importing from it"
    assert verdict.bound_by == [(".venv", "axiom_appkit")]


def test_the_reason_names_the_venv_the_package_and_why_git_missed_it(tmp_path, monkeypatch):
    wt_path = tmp_path / "wt"
    wt_path.mkdir()
    venv = _venv_with_editable(tmp_path, ".venv", "axiom_appkit", wt_path)
    info = worktrees.WorktreeInfo(path=wt_path, branch="feat/x", head_sha="a")
    monkeypatch.setattr(worktrees, "_dirty_status", lambda _p: (False, True))
    monkeypatch.setattr(worktrees, "_branch_exists_on_origin", lambda *_a: True)
    monkeypatch.setattr(worktrees, "_is_ancestor_of_default", lambda *_a: False)
    monkeypatch.setattr(worktrees, "_pr_state_for_branch", lambda *_a: None)

    verdict = worktrees.assess_staleness(info, tmp_path, default_branch="main", venvs=[venv])
    said = " ".join(verdict.reasons)

    assert "axiom_appkit" in said and ".venv" in said
    assert "git cannot see" in said


def test_an_unbound_clean_stale_worktree_is_still_prunable(tmp_path, monkeypatch):
    """The floor must not block everything — it is a floor, not a veto."""
    wt_path = tmp_path / "wt"
    wt_path.mkdir()
    info = worktrees.WorktreeInfo(path=wt_path, branch="feat/done", head_sha="a")
    monkeypatch.setattr(worktrees, "_dirty_status", lambda _p: (False, True))
    monkeypatch.setattr(worktrees, "_branch_exists_on_origin", lambda *_a: False)
    monkeypatch.setattr(worktrees, "_is_ancestor_of_default", lambda *_a: True)
    monkeypatch.setattr(worktrees, "_pr_state_for_branch", lambda *_a: "MERGED")

    verdict = worktrees.assess_staleness(info, tmp_path, default_branch="main", venvs=[])

    assert verdict.is_stale is True and verdict.can_force_prune is True


def test_a_missing_venv_directory_is_not_an_error(tmp_path):
    assert worktrees.editable_bindings(tmp_path, [tmp_path / "nope"]) == []
