# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the local_diff tool."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from axiom.extensions.builtins.review.tools.diff import local_diff


class TestLocalDiff:
    def test_empty_diff_returns_empty_string(self):
        mock_result = MagicMock()
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = local_diff()
            assert result == ""
            mock_run.assert_called_once()

    def test_single_file_diff_has_hunk_header(self):
        fake_diff = (
            "diff --git a/src/foo.py b/src/foo.py\n"
            "index abc..def 100644\n"
            "--- a/src/foo.py\n"
            "+++ b/src/foo.py\n"
            "@@ -10,6 +10,8 @@ def bar():\n"
            " pass\n"
            "+# new line\n"
        )
        mock_result = MagicMock()
        mock_result.stdout = fake_diff
        with patch("subprocess.run", return_value=mock_result):
            result = local_diff()
            assert "@@ -10,6 +10,8 @@" in result
            assert "+++ b/src/foo.py" in result

    def test_multi_file_diff_count(self):
        fake_diff = (
            "diff --git a/a.py b/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
            "diff --git a/b.py b/b.py\n"
            "+++ b/b.py\n"
            "@@ -5,1 +5,2 @@\n"
            " x\n"
            "+y\n"
        )
        mock_result = MagicMock()
        mock_result.stdout = fake_diff
        with patch("subprocess.run", return_value=mock_result):
            result = local_diff()
            assert result.count("+++ b/") == 2

    def test_base_ref_override(self):
        mock_result = MagicMock()
        mock_result.stdout = "some diff"
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            local_diff(base="develop")
            args = mock_run.call_args[0][0]
            assert "develop...HEAD" in args
