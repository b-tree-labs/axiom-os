# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the validator gate."""

from __future__ import annotations

from axiom.extensions.builtins.review.agents.rev_u.validator import validate
from axiom.extensions.builtins.review.tools.findings import Finding


def _f(path="src/foo.py", line=10, severity="minor", pass_kind="correctness"):
    return Finding(
        severity=severity,
        pass_kind=pass_kind,
        path=path,
        line=line,
        message="test",
    )


# A diff that touches src/foo.py lines 9–12 (new-side).
SAMPLE_DIFF = (
    "diff --git a/src/foo.py b/src/foo.py\n"
    "--- a/src/foo.py\n"
    "+++ b/src/foo.py\n"
    "@@ -9,3 +9,4 @@ def foo():\n"
    " context\n"
    "+new_line_10\n"
    "+new_line_11\n"
    " end\n"
)


class TestValidatorDropsOutOfDiffFindings:
    def test_drops_finding_with_unknown_path(self):
        findings = [_f(path="src/not_in_diff.py", line=5)]
        result = validate(findings, SAMPLE_DIFF)
        assert result == []

    def test_preserves_in_diff_finding(self):
        findings = [_f(path="src/foo.py", line=10)]
        result = validate(findings, SAMPLE_DIFF)
        assert len(result) == 1
        assert result[0].path == "src/foo.py"


class TestValidatorNitFloor:
    def test_nit_floor_drops_beyond_20(self):
        # 25 nit findings at in-diff lines
        findings = [_f(path="src/foo.py", line=10, severity="nit") for _ in range(25)]
        result = validate(findings, SAMPLE_DIFF)
        assert len(result) == 20

    def test_non_nit_findings_not_affected_by_floor(self):
        findings = (
            [_f(path="src/foo.py", line=10, severity="nit") for _ in range(25)]
            + [_f(path="src/foo.py", line=10, severity="major")]
        )
        result = validate(findings, SAMPLE_DIFF)
        majors = [f for f in result if f.severity == "major"]
        assert len(majors) == 1


class TestValidatorLineFuzz:
    def test_line_within_fuzz_is_kept(self):
        # Line 12 is 2 away from hunk line 10 — within ±2 tolerance.
        findings = [_f(path="src/foo.py", line=12)]
        result = validate(findings, SAMPLE_DIFF)
        assert len(result) == 1

    def test_line_outside_fuzz_is_dropped(self):
        # Line 50 is far outside the hunk — should be dropped.
        findings = [_f(path="src/foo.py", line=50)]
        result = validate(findings, SAMPLE_DIFF)
        assert result == []


class TestValidatorEmpty:
    def test_empty_findings_no_op(self):
        result = validate([], SAMPLE_DIFF)
        assert result == []

    def test_finding_with_no_line_passes_if_path_in_diff(self):
        findings = [_f(path="src/foo.py", line=None)]
        result = validate(findings, SAMPLE_DIFF)
        assert len(result) == 1
