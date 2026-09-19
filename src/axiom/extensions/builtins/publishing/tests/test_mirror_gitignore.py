# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The mirror's git-repo hygiene (ADR-112 §D5): a mirror inside a git repo keeps
its working artifacts out of the index via a managed .gitignore block, and
git annotation never commits while a conflict is unresolved."""

from __future__ import annotations

from axiom.extensions.builtins.publishing.mirror import MirrorEngine
from axiom.extensions.builtins.publishing.providers.local_editor import (
    LocalFileEditor,
)
from axiom.infra.git import init_repo, run_git


def _repo(tmp_path):
    repo = tmp_path / "repo"
    init_repo(repo)
    run_git(repo, "config", "user.email", "t@example.com")
    run_git(repo, "config", "user.name", "Tester")
    return repo


def _remote(tmp_path, text):
    d = tmp_path / "remote"
    d.mkdir(exist_ok=True)
    r = d / "remote.md"
    LocalFileEditor(path=str(r)).human_save(text)
    return str(r)


def test_conflict_in_repo_writes_ignore_block_and_ignores_the_sidecar(tmp_path):
    repo = _repo(tmp_path)
    remote = _remote(tmp_path, "origin\n")
    mirror = repo / "doc.md"
    state = repo / ".axi" / "publisher" / "mirror-state" / "doc.json"
    engine = MirrorEngine(endpoint=LocalFileEditor(path=remote),
                          mirror_path=mirror, state_path=state)
    engine.reconcile()  # baseline pull

    LocalFileEditor(path=remote).human_save("remote moved\n")
    mirror.write_text("local moved\n")
    assert engine.reconcile().action == "conflict"

    body = (repo / ".gitignore").read_text()
    assert "# >>> axiom mirror (managed) >>>" in body
    for pat in ("*.conflict", "*.conflict.*", "*.mirrormeta.json",
                ".axi/publisher/mirror-state/"):
        assert pat in body
    # the .conflict sidecar exists but git does not see it as untracked
    assert mirror.with_suffix(".md.conflict").exists()
    status = run_git(repo, "status", "--porcelain")
    assert "doc.md.conflict" not in status
    assert ".axi/" not in status  # state dir ignored too


def test_git_annotate_defers_commit_while_conflicted(tmp_path):
    repo = _repo(tmp_path)
    remote = _remote(tmp_path, "origin\n")
    mirror = repo / "doc.md"
    engine = MirrorEngine(
        endpoint=LocalFileEditor(path=remote), mirror_path=mirror,
        state_path=repo / ".axi" / "publisher" / "mirror-state" / "doc.json",
        git_annotate=True)
    engine.reconcile()  # baseline pull commits doc.md
    baseline_head = run_git(repo, "rev-parse", "HEAD")

    LocalFileEditor(path=remote).human_save("remote moved\n")
    mirror.write_text("local moved\n")
    assert engine.reconcile().action == "conflict"

    # no commit was made for the conflict — HEAD is unchanged
    assert run_git(repo, "rev-parse", "HEAD") == baseline_head
    # and a further blocked pass also commits nothing
    engine.reconcile()
    assert run_git(repo, "rev-parse", "HEAD") == baseline_head

    # resolving unblocks; a normal sync annotates again
    engine.resolve("theirs")
    assert "conflict" not in run_git(repo, "log", "-1", "--pretty=%s").lower()
