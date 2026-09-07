# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for grade_push (batch grade push + standalone fallback).

Per spec-classroom.md §3.1 WF-4. Standalone-first: grade push works
without Canvas (writes to local ledger). With Canvas configured,
pushes via LMS adapter; handles partial failures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class FakeLMSGradePush:
    fails_for: set[str] = field(default_factory=set)  # student_ids to fail
    pushed: list[tuple] = field(default_factory=list)

    def push_grade(self, course_id: str, assignment_id: str, student_id: str,
                   score: float, comment: str = ""):
        self.pushed.append((course_id, assignment_id, student_id, score, comment))

        @dataclass
        class Result:
            success: bool
            canvas_submission_id: str | None = None
            message: str = ""

        if student_id in self.fails_for:
            return Result(success=False, message="simulated 500")
        return Result(success=True, canvas_submission_id=f"sub-{student_id}")


def _scored(student_id: str, final_score: float, assessment_id="pre", question_id="Q1"):
    from axiom.extensions.builtins.classroom.quiz_scoring import ScoredResponse

    return ScoredResponse(
        student_id=student_id, assessment_id=assessment_id,
        question_id=question_id, question_type="mcq",
        final_score=final_score, needs_review=False,
    )


class TestStandaloneGradeFallback:
    def test_writes_grade_ledger_when_no_lms(self, tmp_path):
        from axiom.extensions.builtins.classroom.grade_push import push_grades

        scored = [_scored("s1", 0.8), _scored("s2", 0.4)]

        result = push_grades(
            scored_responses=scored,
            assessment_id="pre",
            classroom_id="test-cr",
            canvas_course_id=None,
            canvas_assignment_id=None,
            lms=None,
            local_dir=tmp_path,
        )

        assert result.pushed_count == 0
        assert result.local_logged_count == 2
        assert result.failures == []

        ledger = tmp_path / "classrooms" / "test-cr" / "grades" / "pre.json"
        assert ledger.exists()
        data = json.loads(ledger.read_text())
        assert data["assessment_id"] == "pre"
        assert {g["student_id"] for g in data["grades"]} == {"s1", "s2"}


class TestLMSGradePush:
    def test_pushes_all_grades_to_canvas(self, tmp_path):
        from axiom.extensions.builtins.classroom.grade_push import push_grades

        lms = FakeLMSGradePush()
        scored = [_scored("s1", 0.9), _scored("s2", 0.5)]

        result = push_grades(
            scored_responses=scored,
            assessment_id="pre",
            classroom_id="cr",
            canvas_course_id="CANVAS-101",
            canvas_assignment_id="A1",
            lms=lms,
            local_dir=tmp_path,
        )

        assert result.pushed_count == 2
        assert result.failures == []
        assert len(lms.pushed) == 2
        student_ids_pushed = {p[2] for p in lms.pushed}
        assert student_ids_pushed == {"s1", "s2"}

    def test_partial_failure_captured(self, tmp_path):
        from axiom.extensions.builtins.classroom.grade_push import push_grades

        lms = FakeLMSGradePush(fails_for={"s2"})
        scored = [_scored("s1", 0.9), _scored("s2", 0.5), _scored("s3", 1.0)]

        result = push_grades(
            scored_responses=scored,
            assessment_id="pre",
            classroom_id="cr",
            canvas_course_id="CX",
            canvas_assignment_id="A1",
            lms=lms,
            local_dir=tmp_path,
        )

        assert result.pushed_count == 2
        assert len(result.failures) == 1
        assert result.failures[0]["student_id"] == "s2"
        assert "500" in result.failures[0]["message"]

    def test_idempotent_ledger_write(self, tmp_path):
        """Always writes local ledger too — even on LMS push success."""
        from axiom.extensions.builtins.classroom.grade_push import push_grades

        lms = FakeLMSGradePush()
        scored = [_scored("s1", 0.9)]
        push_grades(
            scored_responses=scored, assessment_id="pre",
            classroom_id="cr", canvas_course_id="X", canvas_assignment_id="A1",
            lms=lms, local_dir=tmp_path,
        )
        ledger = tmp_path / "classrooms" / "cr" / "grades" / "pre.json"
        assert ledger.exists()


class TestAggregation:
    """Multi-question assessment aggregates to one per-student grade push."""

    def test_aggregates_multi_question_to_one_push(self, tmp_path):
        from axiom.extensions.builtins.classroom.grade_push import push_grades

        lms = FakeLMSGradePush()
        scored = [
            _scored("s1", 1.0, question_id="Q1"),
            _scored("s1", 0.5, question_id="Q2"),
            _scored("s2", 0.0, question_id="Q1"),
            _scored("s2", 1.0, question_id="Q2"),
        ]

        result = push_grades(
            scored_responses=scored, assessment_id="pre",
            classroom_id="cr", canvas_course_id="X", canvas_assignment_id="A1",
            lms=lms, local_dir=tmp_path,
        )

        assert result.pushed_count == 2  # one per student, not per question
        by_student = {p[2]: p[3] for p in lms.pushed}
        assert by_student["s1"] == 0.75  # (1.0 + 0.5) / 2
        assert by_student["s2"] == 0.5


class TestSkipUngraded:
    def test_skips_queued_and_likert(self, tmp_path):
        from axiom.extensions.builtins.classroom.grade_push import push_grades
        from axiom.extensions.builtins.classroom.quiz_scoring import ScoredResponse

        lms = FakeLMSGradePush()
        scored = [
            _scored("s1", 1.0),
            # Queued free-text — should not be pushed
            ScoredResponse(
                "s2", "pre", "FT1", "free_text", suggested_score=0.6,
                needs_review=True,
            ),
            # Likert — ungradable
            ScoredResponse("s3", "pre", "L1", "likert"),
        ]
        result = push_grades(
            scored_responses=scored, assessment_id="pre",
            classroom_id="cr", canvas_course_id="X", canvas_assignment_id="A1",
            lms=lms, local_dir=tmp_path,
        )
        assert result.pushed_count == 1
        assert result.skipped_count == 2


class TestFederationAware:
    """Stretch: accept signed grade claims from peer nodes (ADR-023)."""

    def test_accepts_signed_claim_batch(self, tmp_path):
        from axiom.extensions.builtins.classroom.grade_push import push_grade_claims

        claims = [
            {
                "student_id": "s1", "assessment_id": "pre",
                "question_id": "Q1", "question_type": "mcq",
                "final_score": 0.9, "signer_node": "prague.axiom.eu",
                "signature": "valid-sig-stub",
            },
        ]
        # trust_verifier: callable that returns True for valid claims
        result = push_grade_claims(
            claims=claims,
            assessment_id="pre",
            classroom_id="cr",
            canvas_course_id=None,
            canvas_assignment_id=None,
            lms=None,
            local_dir=tmp_path,
            trust_verifier=lambda claim: claim.get("signature") == "valid-sig-stub",
        )
        assert result.local_logged_count == 1
        assert result.rejected_count == 0

    def test_rejects_untrusted_claims(self, tmp_path):
        from axiom.extensions.builtins.classroom.grade_push import push_grade_claims

        claims = [
            {"student_id": "s1", "assessment_id": "pre", "question_id": "Q1",
             "question_type": "mcq", "final_score": 0.9,
             "signer_node": "attacker.example.com", "signature": "bogus"},
        ]
        result = push_grade_claims(
            claims=claims,
            assessment_id="pre",
            classroom_id="cr",
            canvas_course_id=None,
            canvas_assignment_id=None,
            lms=None,
            local_dir=tmp_path,
            trust_verifier=lambda c: False,
        )
        assert result.local_logged_count == 0
        assert result.rejected_count == 1
