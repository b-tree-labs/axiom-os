# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Finding and FindingSet data types."""

from __future__ import annotations

import json

from axiom.extensions.builtins.review.tools.findings import (
    SEVERITY_ORDER,
    Finding,
    FindingSet,
)


def _make_finding(severity="minor", pass_kind="correctness", path="src/foo.py", line=10):
    return Finding(
        severity=severity,
        pass_kind=pass_kind,
        path=path,
        line=line,
        message="test finding",
        suggested_fix="fix it",
    )


class TestFindingDataclass:
    def test_fields_are_set(self):
        f = Finding(
            severity="blocker",
            pass_kind="security",
            path="src/auth.py",
            line=42,
            message="SQL injection risk",
            suggested_fix="Use parameterised queries",
        )
        assert f.severity == "blocker"
        assert f.pass_kind == "security"
        assert f.path == "src/auth.py"
        assert f.line == 42
        assert f.message == "SQL injection risk"
        assert f.suggested_fix == "Use parameterised queries"

    def test_optional_fields_default_to_none(self):
        f = Finding(severity="nit", pass_kind="docs", path="README.md", line=None, message="x")
        assert f.line is None
        assert f.suggested_fix is None

    def test_to_dict_roundtrip(self):
        f = _make_finding()
        d = f.to_dict()
        assert d["severity"] == "minor"
        assert d["pass_kind"] == "correctness"
        f2 = Finding.from_dict(d)
        assert f2 == f

    def test_severity_rank_ordering(self):
        ranks = {s: _make_finding(severity=s).severity_rank() for s in SEVERITY_ORDER}
        assert ranks["blocker"] < ranks["major"] < ranks["minor"] < ranks["nit"]

    def test_unknown_severity_rank_is_high(self):
        f = Finding(severity="unknown", pass_kind="correctness", path="x.py", line=1, message="x")
        assert f.severity_rank() > 10


class TestFindingSetBySeverity:
    def test_groups_by_severity(self):
        fs = FindingSet(findings=[
            _make_finding(severity="blocker"),
            _make_finding(severity="minor"),
            _make_finding(severity="blocker"),
        ])
        groups = fs.by_severity()
        assert len(groups["blocker"]) == 2
        assert len(groups["minor"]) == 1
        assert groups["nit"] == []

    def test_empty_groups_present(self):
        fs = FindingSet()
        groups = fs.by_severity()
        for sev in SEVERITY_ORDER:
            assert sev in groups


class TestFindingSetMerge:
    def test_merge_combines_findings(self):
        a = FindingSet(findings=[_make_finding(severity="blocker")])
        b = FindingSet(findings=[_make_finding(severity="nit"), _make_finding(severity="minor")])
        merged = a.merge(b)
        assert len(merged) == 3

    def test_merge_does_not_mutate_originals(self):
        a = FindingSet(findings=[_make_finding()])
        b = FindingSet(findings=[_make_finding()])
        merged = a.merge(b)
        assert len(a) == 1
        assert len(b) == 1
        assert len(merged) == 2


class TestFindingSetJSON:
    def test_json_roundtrip(self):
        fs = FindingSet(findings=[
            _make_finding(severity="blocker"),
            _make_finding(severity="nit"),
        ])
        serialised = fs.to_json()
        loaded_data = json.loads(serialised)
        assert isinstance(loaded_data, list)
        assert len(loaded_data) == 2

        fs2 = FindingSet.from_json(serialised)
        assert len(fs2) == 2
        assert fs2.findings[0].severity == "blocker"
        assert fs2.findings[1].severity == "nit"

    def test_json_is_valid_json(self):
        fs = FindingSet(findings=[_make_finding()])
        raw = fs.to_json()
        parsed = json.loads(raw)
        assert isinstance(parsed, list)
