# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The managed .gitignore block helper (ADR-112 §D5)."""

from __future__ import annotations

from axiom.infra.git import ensure_managed_gitignore, init_repo

MARK = "axiom mirror"
PATS = ["*.conflict", "*.mirrormeta.json", ".axi/publisher/mirror-state/"]


def _repo(tmp_path):
    init_repo(tmp_path)
    return tmp_path


def test_creates_block_in_fresh_repo(tmp_path):
    repo = _repo(tmp_path)
    gi, changed = ensure_managed_gitignore(repo, marker=MARK, patterns=PATS)
    assert changed is True
    body = gi.read_text()
    assert "# >>> axiom mirror (managed) >>>" in body
    assert "# <<< axiom mirror (managed) <<<" in body
    for p in PATS:
        assert p in body


def test_is_idempotent(tmp_path):
    repo = _repo(tmp_path)
    gi, first = ensure_managed_gitignore(repo, marker=MARK, patterns=PATS)
    before = gi.read_text()
    _, second = ensure_managed_gitignore(repo, marker=MARK, patterns=PATS)
    assert first is True and second is False
    assert gi.read_text() == before  # byte-identical, written once


def test_appends_after_existing_content_without_clobbering(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    gi, changed = ensure_managed_gitignore(repo, marker=MARK, patterns=PATS)
    body = gi.read_text()
    assert changed is True
    assert "__pycache__/" in body and "*.pyc" in body      # existing kept
    assert body.index("__pycache__/") < body.index("# >>> axiom mirror")  # appended after
    assert "\n\n# >>> axiom mirror" in body                # separated by a blank line


def test_rewrites_block_in_place_when_patterns_drift(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("keep-me\n")
    ensure_managed_gitignore(repo, marker=MARK, patterns=["*.old"])
    gi, changed = ensure_managed_gitignore(repo, marker=MARK, patterns=PATS)
    body = gi.read_text()
    assert changed is True
    assert "*.old" not in body            # stale pattern gone
    assert "*.conflict" in body           # new patterns in
    assert "keep-me" in body              # surrounding content preserved
    assert body.count("# >>> axiom mirror (managed) >>>") == 1  # exactly one block


def test_targets_repo_toplevel_even_from_a_subdir(tmp_path):
    repo = _repo(tmp_path)
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    gi, _ = ensure_managed_gitignore(sub, marker=MARK, patterns=PATS)
    assert gi == repo / ".gitignore"      # written at the top level, not in a/b
