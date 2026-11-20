# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for git availability helpers (axiom.infra.git)."""

from __future__ import annotations

import shutil as _shutil

import pytest

from axiom.infra import git as gitmod

requires_git = pytest.mark.skipif(
    _shutil.which("git") is None, reason="git binary not installed"
)


def test_git_available_true(monkeypatch):
    monkeypatch.setattr(gitmod.shutil, "which", lambda _b: "/usr/bin/git")
    assert gitmod.git_available() is True


def test_git_available_false(monkeypatch):
    monkeypatch.setattr(gitmod.shutil, "which", lambda _b: None)
    assert gitmod.git_available() is False


def test_is_git_repo_filesystem_based_without_git_binary(monkeypatch, tmp_path):
    # is_git_repo is a filesystem check — works even without the git binary.
    monkeypatch.setattr(gitmod, "git_available", lambda: False)
    (tmp_path / ".git").mkdir()
    assert gitmod.is_git_repo(tmp_path) is True


@requires_git
def test_is_git_repo_false_for_plain_dir(tmp_path):
    assert gitmod.is_git_repo(tmp_path) is False


@requires_git
def test_is_git_repo_true_after_init(tmp_path):
    gitmod.init_repo(tmp_path)
    assert gitmod.is_git_repo(tmp_path) is True


@requires_git
def test_init_repo_is_idempotent(tmp_path):
    gitmod.init_repo(tmp_path)
    gitmod.init_repo(tmp_path)  # second call must not raise or re-init
    assert gitmod.is_git_repo(tmp_path) is True


@requires_git
def test_is_git_repo_scoped_to_path_not_parent(tmp_path):
    # parent is a repo; child has no .git of its own. The ceiling pins git
    # to `path` and below, so a child dir is not itself a repo root.
    gitmod.init_repo(tmp_path)
    child = tmp_path / "sub"
    child.mkdir()
    assert gitmod.is_git_repo(child) is False


@requires_git
def test_is_inside_work_tree_true_for_child(tmp_path):
    # Unlike is_git_repo, this walks up: a child of a repo IS inside it.
    gitmod.init_repo(tmp_path)
    child = tmp_path / "sub"
    child.mkdir()
    assert gitmod.is_inside_work_tree(child) is True


@requires_git
def test_is_inside_work_tree_false_for_plain_dir(tmp_path):
    assert gitmod.is_inside_work_tree(tmp_path) is False
