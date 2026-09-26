# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``_non_clobbering_path`` — Finder-style name reservation."""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.publishing.engine import _non_clobbering_path


def test_returns_bare_name_when_target_does_not_exist(tmp_path: Path) -> None:
    target = tmp_path / "doc.docx"
    assert _non_clobbering_path(target) == target


def test_appends_first_slot_when_target_exists(tmp_path: Path) -> None:
    target = tmp_path / "doc.docx"
    target.write_bytes(b"")
    assert _non_clobbering_path(target) == tmp_path / "doc (1).docx"


def test_increments_until_free_slot(tmp_path: Path) -> None:
    (tmp_path / "doc.docx").write_bytes(b"")
    (tmp_path / "doc (1).docx").write_bytes(b"")
    (tmp_path / "doc (2).docx").write_bytes(b"")
    assert _non_clobbering_path(tmp_path / "doc.docx") == tmp_path / "doc (3).docx"


def test_handles_compound_extensions(tmp_path: Path) -> None:
    # Path.stem only strips the LAST suffix — confirm we respect that
    # convention (foo.tar.gz → suffix='.gz', stem='foo.tar').
    (tmp_path / "foo.tar.gz").write_bytes(b"")
    out = _non_clobbering_path(tmp_path / "foo.tar.gz")
    assert out == tmp_path / "foo.tar (1).gz"


def test_preserves_parent_directory(tmp_path: Path) -> None:
    sub = tmp_path / "deep" / "nested"
    sub.mkdir(parents=True)
    target = sub / "x.md"
    target.write_bytes(b"")
    assert _non_clobbering_path(target).parent == sub
