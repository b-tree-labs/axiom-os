# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for CLI adoption surfaces: brief, explain, compare (#24, #25, #26)."""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    return tmp_path / "runtime"


class TestBrief:
    def test_brief_runs_on_empty_classroom(self, runtime_dir, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main(["brief", "cr-brief", "--instructor", "@ben:ut"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Classroom: cr-brief" in out
        # Polish pass 379643b rephrased "Open help tickets: 0" into
        # "No open help tickets." — preserved the information, removed
        # the count-in-a-label cadence.
        assert "No open help tickets" in out

    def test_brief_json_format(self, runtime_dir, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main([
            "brief", "cr-brief", "--instructor", "@ben:ut", "--format", "json",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["classroom_id"] == "cr-brief"


class TestExplain:
    def test_explain_no_data(self, runtime_dir, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main([
            "explain", "cr-explain",
            "--student", "s1", "--assessment", "pre", "--question", "Q1",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Grade explanation" in out


class TestCompare:
    def test_compare_empty_students(self, runtime_dir, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main([
            "compare", "cr-comp",
            "--assessment", "pre", "--question", "Q1",
            "--students", "s1,s2",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Answer comparison" in out
        assert "s1" in out
        assert "s2" in out
