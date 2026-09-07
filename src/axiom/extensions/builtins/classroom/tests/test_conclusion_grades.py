# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for wrap grades — FW-4 P4.

Computes per-student final grades from the grade ledger and optionally
pushes them to Canvas via the configured LMS provider.

Push is off by default (compute-only). ``--push`` is explicit.
``--dry-run`` is the old name, kept as an alias.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.classroom.conclusion import (
    compute_final_grades,
    finalize_grades,
)


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    import axiom.extensions.builtins.classroom.operational_store as store

    store._registry = None
    yield tmp_path
    store._registry = None


@pytest.fixture
def demo_with_ledger(_isolated_runtime):
    from axiom.extensions.builtins.classroom.demo import (
        DEMO_CLASSROOM_ID,
        seed_demo,
    )

    seed_demo()
    grades_dir = (
        _isolated_runtime / "classrooms" / DEMO_CLASSROOM_ID / "grades"
    )
    grades_dir.mkdir(parents=True, exist_ok=True)
    (grades_dir / "baseline.json").write_text(
        json.dumps(
            {
                "classroom_id": DEMO_CLASSROOM_ID,
                "assessment_id": "baseline",
                "grades": [
                    {"student_id": "s-alice", "score": 0.80},
                    {"student_id": "s-bob", "score": 0.60},
                ],
            }
        )
    )
    (grades_dir / "midpoint.json").write_text(
        json.dumps(
            {
                "classroom_id": DEMO_CLASSROOM_ID,
                "assessment_id": "midpoint",
                "grades": [
                    {"student_id": "s-alice", "score": 1.00},
                    {"student_id": "s-bob", "score": 0.80},
                ],
            }
        )
    )
    return DEMO_CLASSROOM_ID


# ---------------------------------------------------------------------------
# compute_final_grades — pure compute (no I/O beyond reading ledger)
# ---------------------------------------------------------------------------


class TestComputeFinalGrades:
    def test_returns_per_student_means(self, demo_with_ledger):
        grades = compute_final_grades(demo_with_ledger)
        by_id = {g["student_id"]: g for g in grades["students"]}
        # Alice: (0.80 + 1.00) / 2 = 0.90
        assert by_id["s-alice"]["final_grade"] == pytest.approx(0.90)
        # Bob: (0.60 + 0.80) / 2 = 0.70
        assert by_id["s-bob"]["final_grade"] == pytest.approx(0.70)

    def test_reports_per_student_assessment_count(self, demo_with_ledger):
        grades = compute_final_grades(demo_with_ledger)
        by_id = {g["student_id"]: g for g in grades["students"]}
        assert by_id["s-alice"]["assessments_graded"] == 2

    def test_includes_assessment_breakdown(self, demo_with_ledger):
        grades = compute_final_grades(demo_with_ledger)
        by_id = {g["student_id"]: g for g in grades["students"]}
        breakdown = by_id["s-alice"]["scores"]
        assert breakdown["baseline"] == pytest.approx(0.80)
        assert breakdown["midpoint"] == pytest.approx(1.00)

    def test_empty_ledger_returns_empty_students(self, _isolated_runtime):
        from axiom.extensions.builtins.classroom.demo import (
            DEMO_CLASSROOM_ID,
            seed_demo,
        )

        seed_demo()
        grades = compute_final_grades(DEMO_CLASSROOM_ID)
        assert grades["students"] == []

    def test_unknown_classroom_returns_error(self):
        grades = compute_final_grades("nope")
        assert "error" in grades


# ---------------------------------------------------------------------------
# finalize_grades — compute + (optional) push
# ---------------------------------------------------------------------------


class TestFinalizeGrades:
    def test_dry_run_is_default(self, demo_with_ledger):
        """With push=False, finalize is compute-only; no LMS calls."""
        result = finalize_grades(
            classroom_id=demo_with_ledger, push=False,
        )
        assert result["pushed"] is False
        # Grades still computed
        by_id = {g["student_id"]: g for g in result["students"]}
        assert by_id["s-alice"]["final_grade"] == pytest.approx(0.90)

    def test_push_requires_canvas_assignment_id(self, demo_with_ledger):
        """Pushing without a target assignment is a user error."""
        result = finalize_grades(
            classroom_id=demo_with_ledger,
            push=True,
            canvas_assignment_id=None,
        )
        assert result["pushed"] is False
        assert "canvas_assignment_id" in result.get("error", "").lower()

    def test_push_with_mock_provider(self, demo_with_ledger):
        class _MockProvider:
            def __init__(self):
                self.pushed = []

            def push_grade(
                self, course_id, assignment_id, student_id, score, comment="",
            ):
                self.pushed.append(
                    (course_id, assignment_id, student_id, score)
                )

                class _R:
                    success = True
                    message = ""
                    canvas_submission_id = f"sub-{student_id}"

                return _R()

        provider = _MockProvider()
        result = finalize_grades(
            classroom_id=demo_with_ledger,
            push=True,
            canvas_course_id="canvas-course-1",
            canvas_assignment_id="canvas-asgn-final",
            provider=provider,
        )
        assert result["pushed"] is True
        assert len(provider.pushed) == 2  # alice + bob
        # Scores pushed are the computed finals
        by_student = {row[2]: row[3] for row in provider.pushed}
        assert by_student["s-alice"] == pytest.approx(0.90)

    def test_push_records_failures(self, demo_with_ledger):
        class _FailingProvider:
            def push_grade(self, *a, **kw):
                class _R:
                    success = False
                    message = "Canvas 500"
                    canvas_submission_id = None

                return _R()

        result = finalize_grades(
            classroom_id=demo_with_ledger,
            push=True,
            canvas_course_id="c1",
            canvas_assignment_id="a1",
            provider=_FailingProvider(),
        )
        assert result["pushed"] is True  # we tried
        assert len(result["failures"]) == 2
        assert result["failures"][0]["error"]


# ---------------------------------------------------------------------------
# CLI — axi classroom wrap grades
# ---------------------------------------------------------------------------


class TestGradesCLI:
    def test_dry_run_default_json(self, demo_with_ledger, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main(
            ["wrap", "grades", demo_with_ledger, "--json"]
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["pushed"] is False
        assert len(data["students"]) == 2

    def test_markdown_lists_student_finals(self, demo_with_ledger, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main(["wrap", "grades", demo_with_ledger])
        assert rc == 0
        out = capsys.readouterr().out
        assert "s-alice" in out or "Alice" in out
        assert "0.9" in out

    def test_push_without_assignment_fails(self, demo_with_ledger, capsys):
        from axiom.extensions.builtins.classroom.cli import main

        rc = main(
            ["wrap", "grades", demo_with_ledger, "--push", "--json"]
        )
        assert rc == 1
        data = json.loads(capsys.readouterr().out)
        assert data["pushed"] is False


# ---------------------------------------------------------------------------
# Chat tool — classroom_wrap_grades
# ---------------------------------------------------------------------------


class TestGradesChatTool:
    def test_tool_registered(self):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        names = {t.name for t in prep_tools.TOOLS}
        assert "classroom_wrap_grades" in names

    def test_dry_run_default(self, demo_with_ledger):
        from axiom.extensions.builtins.classroom.chat_tools import prep_tools

        result = prep_tools.execute(
            "classroom_wrap_grades", {"classroom_id": demo_with_ledger},
        )
        assert result["pushed"] is False
        assert len(result["students"]) == 2
