# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for side-by-side answer comparison (#25)."""

from __future__ import annotations

import pytest


@pytest.fixture
def composition(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    from axiom.extensions.builtins.classroom.composition_boot import (
        build_classroom_composition,
    )

    return build_classroom_composition(classroom_id="cr-compare")


def _scored(student_id, final):
    from axiom.extensions.builtins.classroom.quiz_scoring import ScoredResponse

    return ScoredResponse(
        student_id=student_id, assessment_id="pre", question_id="Q1",
        question_type="free_text", final_score=final,
        needs_review=False,
    )


def _seed_score(composition, student_id, final):
    from axiom.extensions.builtins.classroom.quiz_scoring import (
        record_scored_response,
    )

    record_scored_response(
        composition=composition, scored=_scored(student_id, final),
        classroom_id="cr-compare",
    )


class TestComparison:
    def test_empty_cohort_empty_rows(self, composition):
        from axiom.extensions.builtins.classroom.compare_answers import (
            compare_answers,
        )

        result = compare_answers(
            composition, assessment_id="pre", question_id="Q1",
            student_ids=[],
        )
        assert result.rows == []
        assert result.score_spread is None

    def test_scores_compared_across_students(self, composition):
        from axiom.extensions.builtins.classroom.compare_answers import (
            compare_answers,
        )

        _seed_score(composition, "s1", 1.0)
        _seed_score(composition, "s2", 0.5)
        _seed_score(composition, "s3", 0.0)

        result = compare_answers(
            composition, assessment_id="pre", question_id="Q1",
            student_ids=["s1", "s2", "s3"],
        )
        assert len(result.rows) == 3
        scores = [r.final_score for r in result.rows]
        assert set(scores) == {0.0, 0.5, 1.0}
        assert result.score_spread == 1.0

    def test_missing_student_has_empty_row(self, composition):
        from axiom.extensions.builtins.classroom.compare_answers import (
            compare_answers,
        )

        _seed_score(composition, "s1", 1.0)

        result = compare_answers(
            composition, assessment_id="pre", question_id="Q1",
            student_ids=["s1", "s2"],
        )
        s2 = next(r for r in result.rows if r.student_id == "s2")
        assert s2.final_score is None
        assert s2.answer is None

    def test_latest_score_used_under_supersedure(self, composition):
        from axiom.extensions.builtins.classroom.compare_answers import (
            compare_answers,
        )

        _seed_score(composition, "s1", 0.5)
        _seed_score(composition, "s1", 1.0)  # override

        result = compare_answers(
            composition, assessment_id="pre", question_id="Q1",
            student_ids=["s1"],
        )
        assert result.rows[0].final_score == 1.0


class TestMarkdown:
    def test_render_markdown(self, composition):
        from axiom.extensions.builtins.classroom.compare_answers import (
            compare_answers,
            render_markdown,
        )

        _seed_score(composition, "s1", 1.0)
        _seed_score(composition, "s2", 0.5)

        comp = compare_answers(
            composition, assessment_id="pre", question_id="Q1",
            student_ids=["s1", "s2"],
        )
        md = render_markdown(comp)
        assert "Answer comparison" in md
        assert "s1" in md
        assert "s2" in md
        assert "Score spread" in md


class TestWithTraces:
    """When episodic trace fragments exist for the question, their
    `response` text appears in the answer column."""

    def test_trace_response_surfaces(self, composition, tmp_path, monkeypatch):
        from axiom.extensions.builtins.classroom.compare_answers import (
            compare_answers,
        )

        _seed_score(composition, "s1", 0.8)

        # Seed an episodic trace for s1 answering Q1
        composition.write(
            content={
                "event_time": "2026-04-17T10:00:00Z",
                "session_type": "quiz",
                "classroom_id": "cr-compare",
                "course_id": "c",
                "assessment_id": "pre",
                "question_id": "Q1",
                "response": "Fission splits heavy nuclei.",
            },
            cognitive_type="episodic",
            principal_id="s1",
            agents=set(),
            resources=set(),
        )

        result = compare_answers(
            composition, assessment_id="pre", question_id="Q1",
            student_ids=["s1"],
        )
        assert result.rows[0].answer == "Fission splits heavy nuclei."
