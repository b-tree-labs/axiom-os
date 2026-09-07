# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the gather_context tool."""

from __future__ import annotations

from axiom.extensions.builtins.review.tools.context import gather_context
from axiom.extensions.builtins.review.tools.findings import Finding


def _make_diff(*paths: str) -> str:
    """Build a minimal diff string that touches the given paths."""
    parts = []
    for p in paths:
        parts.append(
            f"diff --git a/{p} b/{p}\n"
            f"--- a/{p}\n"
            f"+++ b/{p}\n"
            f"@@ -1,1 +1,1 @@\n"
            f"-old\n"
            f"+new\n"
        )
    return "".join(parts)


class TestGatherContext:
    def test_gathers_full_file_contents(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.py").write_text("def foo(): pass\n")
        diff = _make_diff("src/foo.py")
        ctx, warnings = gather_context(diff, repo_root=str(tmp_path))
        assert "src/foo.py" in ctx
        assert "def foo(): pass" in ctx["src/foo.py"]
        assert warnings == []

    def test_cap_at_50_files_returns_warning_finding(self):
        paths = [f"src/file_{i}.py" for i in range(51)]
        diff = _make_diff(*paths)
        ctx, warnings = gather_context(diff, repo_root="/nonexistent/root")
        assert ctx == {}
        assert len(warnings) == 1
        assert isinstance(warnings[0], Finding)
        assert "too large" in warnings[0].message

    def test_missing_files_handled_gracefully(self, tmp_path):
        diff = _make_diff("src/deleted_file.py")
        ctx, warnings = gather_context(diff, repo_root=str(tmp_path))
        assert "src/deleted_file.py" not in ctx
        assert warnings == []

    def test_binary_files_skipped_silently(self, tmp_path):
        (tmp_path / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
        diff = _make_diff("img.png")
        ctx, warnings = gather_context(diff, repo_root=str(tmp_path))
        assert "img.png" not in ctx
        assert warnings == []

    def test_symlinks_skipped_silently(self, tmp_path):
        target = tmp_path / "real.py"
        target.write_text("x = 1\n")
        link = tmp_path / "link.py"
        link.symlink_to(target)
        diff = _make_diff("link.py")
        ctx, warnings = gather_context(diff, repo_root=str(tmp_path))
        assert "link.py" not in ctx
        assert warnings == []
