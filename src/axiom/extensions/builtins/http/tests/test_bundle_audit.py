# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A committed ``dist/`` can go internally inconsistent, and nothing notices.

The build output of every appkit-shell surface is tracked in git, which
means a branch switch replaces ``index.html`` with the version that
branch committed while another build's assets stay on disk beside it.
The node then serves an index that names hashes from one build next to
files from another. Everything answers 200, every test passes, and the
browser gets an app whose stylesheet does not exist.

``audit_bundle`` is what turns that into a failing test instead of a
founder's screenshot. It walks reachability transitively, because a
lazily-imported chunk is named by its parent chunk and not by the index.
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.http.spa import audit_bundle


def _write(dist: Path, index_refs: list[str], files: dict[str, str]) -> Path:
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    refs = "".join(f'<script src="/s/assets/{r}"></script>' for r in index_refs)
    (dist / "index.html").write_text(f"<!doctype html><html><head>{refs}</head></html>", "utf-8")
    for name, body in files.items():
        (dist / "assets" / name).write_text(body, encoding="utf-8")
    return dist


def test_a_matched_build_is_consistent(tmp_path):
    dist = _write(tmp_path, ["app-AAA.js", "app-AAA.css"], {"app-AAA.js": "1", "app-AAA.css": "x"})
    audit = audit_bundle(dist)
    assert audit.consistent, audit.why()


def test_an_index_naming_an_absent_stylesheet_is_caught(tmp_path):
    """The exact shape of 'the app has no styling applied': the document
    is from one build, the file it names belongs to another."""
    dist = _write(tmp_path, ["app-AAA.js", "app-OLD.css"], {"app-AAA.js": "1", "app-NEW.css": "x"})
    audit = audit_bundle(dist)
    assert not audit.consistent
    assert "app-OLD.css" in audit.missing
    assert "app-NEW.css" in audit.orphaned
    assert "app-OLD.css" in audit.why() and "app-NEW.css" in audit.why()


def test_a_lazy_chunk_is_reachable_through_its_parent(tmp_path):
    """Following only the index would call every lazy chunk dead weight
    and make the guard cry wolf on a perfectly good build."""
    dist = _write(
        tmp_path,
        ["app-AAA.js"],
        {"app-AAA.js": 'import("./Markdown-BBB.js")', "Markdown-BBB.js": "export {}"},
    )
    audit = audit_bundle(dist)
    assert audit.consistent, audit.why()


def test_a_leftover_file_from_an_older_build_is_reported(tmp_path):
    dist = _write(tmp_path, ["app-AAA.js"], {"app-AAA.js": "1", "app-PREVIOUS.js": "0"})
    assert audit_bundle(dist).orphaned == frozenset({"app-PREVIOUS.js"})


def test_an_unbuilt_surface_is_not_an_inconsistent_one(tmp_path):
    """No index at all is the honest not-built state the mount already
    handles — it must not be reported as a broken build."""
    audit = audit_bundle(tmp_path / "never-built")
    assert audit.missing == frozenset() and audit.orphaned == frozenset()
