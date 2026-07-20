# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for wrap analytics — FW-4 P2.

Analytics is a read-only cohort summary. Works for any classroom state
(published or archived). Aggregates what's actually in the operational
store + grade ledger; no backfill magic.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.classroom.conclusion import (
    summarize_classroom,
)


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    import axiom.extensions.builtins.classroom.operational_store as store

    store._registry = None
    yield tmp_path
    store._registry = None


@pytest.fixture
def demo_classroom(_isolated_runtime):
    from axiom.extensions.builtins.classroom.demo import (
        DEMO_CLASSROOM_ID,
        seed_demo,
    )

    seed_demo()
    return DEMO_CLASSROOM_ID


# ---------------------------------------------------------------------------
# summarize_classroom
# ---------------------------------------------------------------------------


class TestSummarizeClassroom:
    def test_returns_classroom_id_and_state(self, demo_classroom):
        result = summarize_classroom(demo_classroom)
        assert result["classroom_id"] == demo_classroom
        assert result["state"] in ("unpublished", "published", "archived")

    def test_roster_summary(self, demo_classroom):
        result = summarize_classroom(demo_classroom)
        assert result["roster"]["size"] == 5  # demo has 5 students
        assert len(result["roster"]["student_ids"]) == 5

    def test_course_config_snapshot(self, demo_classroom):
        result = summarize_classroom(demo_classroom)
        cfg = result["course_config"]
        assert cfg["checkpoints"] == 3  # demo ships with 3 defaults
        assert cfg["assessments"] == 2  # demo ships baseline + midpoint
        assert cfg["rails"] >= 1
        assert cfg["has_system_prompt"] is True

    def test_grade_ledger_empty_for_fresh_demo(self, demo_classroom):
        """No grades have been pushed — ledger is empty."""
        result = summarize_classroom(demo_classroom)
        assert result["grade_ledger"]["assessments"] == []
        assert result["grade_ledger"]["total_graded"] == 0

    def test_grade_ledger_reads_actual_ledger(
        self, demo_classroom, _isolated_runtime,
    ):
        """If ledger files exist, analytics picks them up + aggregates."""
        runtime_root = _isolated_runtime
        grades_dir = (
            runtime_root
            / "classrooms" / demo_classroom / "grades"
        )
        grades_dir.mkdir(parents=True, exist_ok=True)
        (grades_dir / "baseline.json").write_text(
            json.dumps(
                {
                    "classroom_id": demo_classroom,
                    "assessment_id": "baseline",
                    "grades": [
                        {"student_id": "s-alice", "score": 0.85},
                        {"student_id": "s-bob", "score": 0.70},
                        {"student_id": "s-carol", "score": 0.92},
                    ],
                }
            )
        )

        result = summarize_classroom(demo_classroom)
        assert result["grade_ledger"]["total_graded"] == 3
        assessments = {a["assessment_id"]: a for a in result["grade_ledger"]["assessments"]}
        baseline = assessments["baseline"]
        assert baseline["count"] == 3
        assert 0.80 <= baseline["mean"] <= 0.85
        assert baseline["min"] == 0.70
        assert baseline["max"] == 0.92

    def test_published_timestamp_included(self, demo_classroom):
        from axiom.extensions.builtins.classroom.publish import publish_classroom

        publish_classroom(classroom_id=demo_classroom, approver="@ben:ut")
        result = summarize_classroom(demo_classroom)
        assert result["state"] == "published"
        assert result["published_at"]

    def test_archived_timestamp_included(self, demo_classroom):
        from axiom.extensions.builtins.classroom.archive import archive_classroom
        from axiom.extensions.builtins.classroom.publish import publish_classroom

        publish_classroom(classroom_id=demo_classroom, approver="@ben:ut")
        archive_classroom(
            classroom_id=demo_classroom, archiver="@ben:ut", reason="done",
        )
        result = summarize_classroom(demo_classroom)
        assert result["state"] == "archived"
        assert result["archived_at"]

    def test_unknown_classroom_returns_error(self):
        result = summarize_classroom("nope")
        assert "error" in result


# ---------------------------------------------------------------------------
# CLI — axi classroom wrap analytics <id>
# ---------------------------------------------------------------------------


class TestAnalyticsCLI:
    def test_json_output(self, demo_classroom, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main(
            ["wrap", "analytics", demo_classroom, "--json"]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["classroom_id"] == demo_classroom
        assert data["roster"]["size"] == 5

    def test_markdown_output(self, demo_classroom, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main(["wrap", "analytics", demo_classroom])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Roster" in out or "roster" in out
        assert "5" in out  # roster size


# ---------------------------------------------------------------------------
# Chat tool — classroom_wrap_analytics
# ---------------------------------------------------------------------------


class TestAnalyticsChatTool:
    def test_tool_registered(self):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        names = {t.name for t in prep_tools.TOOLS}
        assert "classroom_wrap_analytics" in names

    def test_tool_returns_summary(self, demo_classroom):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_wrap_analytics", {"classroom_id": demo_classroom},
        )
        assert result["roster"]["size"] == 5

    def test_missing_classroom_error(self):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute("classroom_wrap_analytics", {})
        assert "error" in result
