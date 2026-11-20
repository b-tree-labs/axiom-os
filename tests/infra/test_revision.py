# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axiom.infra.revision``: what a process loaded, read without a subprocess (ADR-202)."""

from __future__ import annotations

from pathlib import Path

from axiom.infra import revision

SHA_A = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
SHA_B = "b2c3d4e5f60718293a4b5c6d7e8f9012345678a1"


def _plain_checkout(root: Path, *, branch: str = "main", sha: str = SHA_A) -> Path:
    git = root / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
    (git / "refs" / "heads" / branch).write_text(f"{sha}\n", encoding="utf-8")
    return root


def test_head_of_a_plain_checkout_from_a_nested_path(tmp_path):
    root = _plain_checkout(tmp_path / "repo")
    nested = root / "src" / "axiom"
    nested.mkdir(parents=True)
    assert revision.checkout_head(nested) == SHA_A


def test_head_of_a_worktree_reads_the_shared_refs(tmp_path):
    main = _plain_checkout(tmp_path / "main", branch="main", sha=SHA_A)
    (main / ".git" / "refs" / "heads" / "feature").write_text(f"{SHA_B}\n", encoding="utf-8")
    private = main / ".git" / "worktrees" / "wt"
    private.mkdir(parents=True)
    (private / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
    (private / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {private}\n", encoding="utf-8")

    assert revision.checkout_head(wt / "src") == SHA_B


def test_a_detached_head_is_the_commit_itself(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text(f"{SHA_B}\n", encoding="utf-8")
    assert revision.checkout_head(root) == SHA_B


def test_a_packed_ref_is_found(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / ".git" / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted \n"
        f"{SHA_A} refs/heads/other\n"
        f"{SHA_B} refs/heads/main\n"
        f"^{SHA_A}\n",
        encoding="utf-8",
    )
    assert revision.checkout_head(root) == SHA_B


def test_no_checkout_means_no_commit(tmp_path):
    (tmp_path / "plain").mkdir()
    assert revision.checkout_head(tmp_path / "plain") is None


def test_the_loaded_revision_describes_this_process():
    loaded = revision.loaded_revision()
    assert loaded.source.endswith("axiom")
    assert loaded.version == revision.installed_version()
    assert loaded.short.startswith(loaded.version or "unknown")
    assert set(loaded.to_dict()) == {"version", "source", "commit", "loaded_at"}


def test_short_form():
    assert revision.Revision(version="1.2.3", source="s", commit="abcdef0123456789").short == "1.2.3@abcdef012"
    assert revision.Revision(version="1.2.3", source="s", commit=None).short == "1.2.3"
    assert revision.Revision(version=None, source="s", commit=None).short == "unknown"
