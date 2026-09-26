# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for grade_explain — one-click provenance trace (#26)."""

from __future__ import annotations

import pytest


@pytest.fixture
def composition(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    from axiom.extensions.builtins.classroom.composition_boot import (
        build_classroom_composition,
    )

    return build_classroom_composition(classroom_id="cr-explain")


def _scored(student_id, final_score, reviewed_by=None, suggested_score=None):
    from axiom.extensions.builtins.classroom.quiz_scoring import ScoredResponse

    return ScoredResponse(
        student_id=student_id, assessment_id="pre",
        question_id="Q1", question_type="mcq",
        auto_score=final_score if not reviewed_by else None,
        suggested_score=suggested_score,
        final_score=final_score,
        needs_review=False,
        reviewed_by=reviewed_by,
    )


class TestBasic:
    def test_no_data_returns_empty_explanation(self, composition):
        from axiom.extensions.builtins.classroom.grade_explain import explain_grade

        exp = explain_grade(
            composition,
            student_id="ghost", assessment_id="none", question_id="none",
        )
        assert exp.student_id == "ghost"
        assert exp.score_fragment is None
        assert exp.response_trace is None
        assert exp.override_events == []


class TestScoreFragment:
    def test_finds_score_fragment(self, composition):
        from axiom.extensions.builtins.classroom.grade_explain import explain_grade
        from axiom.extensions.builtins.classroom.quiz_scoring import (
            record_scored_response,
        )

        record_scored_response(
            composition=composition,
            scored=_scored("s1", 1.0),
            classroom_id="cr-explain",
        )
        exp = explain_grade(
            composition, student_id="s1",
            assessment_id="pre", question_id="Q1",
        )
        assert exp.score_fragment is not None
        assert exp.score_fragment["content"]["final_score"] == 1.0

    def test_latest_score_wins_on_supersedure(self, composition):
        from axiom.extensions.builtins.classroom.grade_explain import explain_grade
        from axiom.extensions.builtins.classroom.quiz_scoring import (
            record_scored_response,
        )

        record_scored_response(
            composition=composition,
            scored=_scored("s1", 0.6),
            classroom_id="cr-explain",
        )
        record_scored_response(
            composition=composition,
            scored=_scored("s1", 0.9, reviewed_by="@instr"),
            classroom_id="cr-explain",
        )
        exp = explain_grade(
            composition, student_id="s1",
            assessment_id="pre", question_id="Q1",
        )
        assert exp.score_fragment["content"]["final_score"] == 0.9


class TestOverrideEvents:
    def test_reviewed_by_surfaces_as_override(self, composition):
        from axiom.extensions.builtins.classroom.grade_explain import explain_grade
        from axiom.extensions.builtins.classroom.quiz_scoring import (
            record_scored_response,
        )

        record_scored_response(
            composition=composition,
            scored=_scored("s1", 0.9, reviewed_by="@ben:ut",
                           suggested_score=0.6),
            classroom_id="cr-explain",
        )
        exp = explain_grade(
            composition, student_id="s1",
            assessment_id="pre", question_id="Q1",
        )
        assert len(exp.override_events) == 1
        ov = exp.override_events[0]
        assert ov["reviewed_by"] == "@ben:ut"
        assert ov["final_score"] == 0.9
        assert ov["prior_score"] == 0.6


class TestAuditTrail:
    def test_audit_entries_included(self, composition):
        from axiom.extensions.builtins.classroom.grade_explain import explain_grade
        from axiom.extensions.builtins.classroom.quiz_scoring import (
            record_scored_response,
        )

        record_scored_response(
            composition=composition,
            scored=_scored("s1", 1.0),
            classroom_id="cr-explain",
        )
        exp = explain_grade(
            composition, student_id="s1",
            assessment_id="pre", question_id="Q1",
        )
        # At least one audit entry (the write)
        assert len(exp.audit_entries) >= 1
        assert exp.audit_entries[0]["entry_type"] == "write"


class TestMarkdown:
    def test_render_without_data(self):
        from axiom.extensions.builtins.classroom.grade_explain import (
            GradeExplanation,
            render_markdown,
        )

        md = render_markdown(GradeExplanation(
            student_id="s1", assessment_id="pre", question_id="Q1",
        ))
        assert "s1" in md
        assert "No score recorded" in md

    def test_render_with_full_data(self, composition):
        from axiom.extensions.builtins.classroom.grade_explain import (
            explain_grade,
            render_markdown,
        )
        from axiom.extensions.builtins.classroom.quiz_scoring import (
            record_scored_response,
        )

        record_scored_response(
            composition=composition,
            scored=_scored("s1", 0.9, reviewed_by="@ben:ut",
                           suggested_score=0.6),
            classroom_id="cr-explain",
        )
        exp = explain_grade(
            composition, student_id="s1",
            assessment_id="pre", question_id="Q1",
        )
        md = render_markdown(exp)
        # Markdown includes all sections
        assert "# Grade explanation" in md
        assert "0.9" in md
        assert "Override events" in md
        assert "@ben:ut" in md
        assert "Audit trail" in md
