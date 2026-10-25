# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""/review gathers its diff with read-only git, bounded and with credential-like files left out."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from axiom.extensions.builtins.chat.commands_ext import review as rv


def _git(root: Path, *a: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
        capture_output=True, text=True, check=True,
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("one\n")
    (root / "b.txt").write_text("bee\n")
    (root / ".env").write_text("TOKEN=old\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    return root


def test_default_covers_staged_and_unstaged_and_lists_untracked(repo):
    (repo / "a.txt").write_text("one\ntwo\n")  # unstaged
    (repo / "b.txt").write_text("bee\nbuzz\n")
    _git(repo, "add", "b.txt")  # staged
    (repo / "new.txt").write_text("fresh\n")  # untracked
    g = rv.gather_diff(repo, [])
    assert "+two" in g.text and "+buzz" in g.text
    assert g.untracked == ("new.txt",)


def test_staged_only_excludes_unstaged(repo):
    (repo / "a.txt").write_text("one\ntwo\n")
    (repo / "b.txt").write_text("bee\nbuzz\n")
    _git(repo, "add", "b.txt")
    g = rv.gather_diff(repo, ["--staged"])
    assert "+buzz" in g.text and "+two" not in g.text


def test_path_limits_the_diff(repo):
    (repo / "a.txt").write_text("one\ntwo\n")
    (repo / "b.txt").write_text("bee\nbuzz\n")
    g = rv.gather_diff(repo, ["a.txt"])
    assert "+two" in g.text and "+buzz" not in g.text


def test_branch_diffs_since_divergence(repo):
    _git(repo, "checkout", "-qb", "feature")
    (repo / "a.txt").write_text("one\nfeat\n")
    _git(repo, "commit", "-qam", "feat")
    g = rv.gather_diff(repo, ["--branch", "main"])
    assert "+feat" in g.text


def test_unknown_branch_and_option_like_names_are_refused(repo):
    with pytest.raises(rv.ReviewError, match="no such branch"):
        rv.gather_diff(repo, ["--branch", "nope"])
    with pytest.raises(rv.ReviewError, match="not a branch name"):
        rv.gather_diff(repo, ["--branch", "--output=/tmp/x"])
    with pytest.raises(rv.ReviewError, match="unknown option"):
        rv.gather_diff(repo, ["--ext-diff"])


def test_path_outside_project_is_refused(repo, tmp_path):
    (tmp_path / "outside.txt").write_text("x")
    with pytest.raises(rv.ReviewError, match="outside the project"):
        rv.gather_diff(repo, ["../outside.txt"])


def test_not_a_repo(tmp_path):
    with pytest.raises(rv.ReviewError, match="not inside a git repository"):
        rv.gather_diff(tmp_path, [])


def test_large_diff_is_truncated_with_the_limit_stated(repo):
    (repo / "a.txt").write_text("".join(f"line {i}\n" for i in range(5000)))
    g = rv.gather_diff(repo, [], limit=2000)
    assert len(g.text) == 2000 and g.truncated_from > 2000
    block = rv.render_diff_block(g, limit=2000)
    assert "truncated to the first 2,000" in block and f"{g.truncated_from:,}" in block


def test_small_diff_is_not_marked_truncated(repo):
    (repo / "a.txt").write_text("one\ntwo\n")
    g = rv.gather_diff(repo, [])
    assert g.truncated_from == 0
    assert "truncated" not in rv.render_diff_block(g)


def test_credential_files_never_reach_the_diff_but_are_named_as_excluded(repo):
    (repo / ".env").write_text("TOKEN=hunter2-new-secret\n")
    (repo / "a.txt").write_text("one\ntwo\n")
    g = rv.gather_diff(repo, [])
    assert "hunter2" not in g.text and "TOKEN" not in g.text
    assert g.excluded
    assert "excluded" in rv.render_diff_block(g)
    assert "+two" in g.text


def test_gathering_does_not_touch_the_repository(repo):
    (repo / "a.txt").write_text("one\ntwo\n")
    before = (_git(repo, "status", "--porcelain"), _git(repo, "rev-parse", "HEAD"),
              (repo / ".git" / "index").read_bytes())
    rv.gather_diff(repo, [])
    rv.gather_diff(repo, ["--staged"])
    after = (_git(repo, "status", "--porcelain"), _git(repo, "rev-parse", "HEAD"),
             (repo / ".git" / "index").read_bytes())
    assert before == after
