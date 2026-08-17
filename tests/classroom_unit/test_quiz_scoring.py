# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for quiz scoring queue + auto-score (WF-4).

Per spec-classroom.md §3.1 WF-4:
  "Auto-scores objective questions, presents free-response scoring
   queue to instructor with LLM-suggested scores, computes aggregate
   statistics, pushes grades to Canvas via API."

Scope here: auto-score, LLM-suggested score for free-text, scoring
queue, instructor override, aggregate stats. Canvas push is in #5.
Standalone-first: auto-score and queue work without Canvas.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeLLMGrader:
    score: float = 0.8
    rationale: str = "Reasonable answer; covers the main concept."
    calls: list = field(default_factory=list)

    def __call__(self, question: str, answer: str, rubric: dict, **kw) -> dict:
        self.calls.append({"question": question, "answer": answer, "rubric": rubric})
        return {"score": self.score, "rationale": self.rationale}


class TestAutoScoreObjective:
    def test_mcq_correct_answer_scores_1(self):
        from axiom.extensions.builtins.classroom.quiz_scoring import auto_score

        response = {
            "student_id": "s1", "assessment_id": "pre-quiz",
            "question_id": "Q1", "question_type": "mcq", "answer": "B",
        }
        result = auto_score(response, answer_key={"Q1": "B"})
        assert result.auto_score == 1.0
        assert result.final_score == 1.0
        assert result.needs_review is False

    def test_mcq_wrong_answer_scores_0(self):
        from axiom.extensions.builtins.classroom.quiz_scoring import auto_score

        response = {
            "student_id": "s1", "assessment_id": "pre-quiz",
            "question_id": "Q1", "question_type": "mcq", "answer": "A",
        }
        result = auto_score(response, answer_key={"Q1": "B"})
        assert result.auto_score == 0.0

    def test_yes_no_match(self):
        from axiom.extensions.builtins.classroom.quiz_scoring import auto_score

        response = {"student_id": "s", "assessment_id": "q", "question_id": "Q",
                    "question_type": "yes_no", "answer": "yes"}
        assert auto_score(response, {"Q": "yes"}).auto_score == 1.0
        response["answer"] = "no"
        assert auto_score(response, {"Q": "yes"}).auto_score == 0.0

    def test_likert_records_but_does_not_score(self):
        from axiom.extensions.builtins.classroom.quiz_scoring import auto_score

        response = {"student_id": "s", "assessment_id": "s", "question_id": "nps",
                    "question_type": "likert", "answer": 7}
        result = auto_score(response, answer_key={})
        assert result.auto_score is None
        assert result.needs_review is False
        assert result.final_score is None


class TestFreeTextLLMGrading:
    def test_free_text_with_rubric_gets_suggested_score(self):
        from axiom.extensions.builtins.classroom.quiz_scoring import auto_score

        response = {
            "student_id": "s1", "assessment_id": "q", "question_id": "FT1",
            "question_type": "free_text",
            "answer": "Fission is splitting heavy nuclei.",
            "question": "What is fission?",
            "rubric": {"max_score": 1.0, "criteria": ["splitting", "heavy nucleus"]},
        }
        grader = FakeLLMGrader(score=0.9, rationale="Hits both criteria.")
        result = auto_score(response, answer_key={}, llm_grader=grader)
        assert result.suggested_score == 0.9
        assert result.needs_review is True
        assert result.final_score is None
        assert result.rationale == "Hits both criteria."

    def test_free_text_without_grader_queued_no_suggestion(self):
        from axiom.extensions.builtins.classroom.quiz_scoring import auto_score

        response = {"student_id": "s", "assessment_id": "q", "question_id": "FT",
                    "question_type": "free_text", "answer": "ok"}
        result = auto_score(response, answer_key={})
        assert result.suggested_score is None
        assert result.needs_review is True


class TestScoringQueue:
    def test_queue_contains_only_pending_free_text(self):
        from axiom.extensions.builtins.classroom.quiz_scoring import (
            auto_score,
            scoring_queue,
        )

        mcq = auto_score(
            {"student_id": "s", "assessment_id": "q", "question_id": "Q1",
             "question_type": "mcq", "answer": "B"},
            answer_key={"Q1": "B"},
        )
        ft = auto_score(
            {"student_id": "s", "assessment_id": "q", "question_id": "FT",
             "question_type": "free_text", "answer": "x"},
            answer_key={},
        )
        queue = scoring_queue([mcq, ft])
        assert len(queue) == 1
        assert queue[0].question_id == "FT"

    def test_queue_excludes_reviewed(self):
        from axiom.extensions.builtins.classroom.quiz_scoring import (
            auto_score,
            override_score,
            scoring_queue,
        )

        ft = auto_score(
            {"student_id": "s", "assessment_id": "q", "question_id": "FT",
             "question_type": "free_text", "answer": "x"},
            answer_key={},
        )
        reviewed = override_score(ft, final=0.75, reviewer="ben@ut.edu")
        assert len(scoring_queue([reviewed])) == 0


class TestOverrideScore:
    def test_instructor_override_sets_final(self):
        from axiom.extensions.builtins.classroom.quiz_scoring import (
            auto_score,
            override_score,
        )

        ft = auto_score(
            {"student_id": "s", "assessment_id": "q", "question_id": "FT",
             "question_type": "free_text", "answer": "x"},
            answer_key={},
            llm_grader=FakeLLMGrader(score=0.6),
        )
        result = override_score(ft, final=0.8, reviewer="ben@ut.edu",
                                note="Partial credit for splitting.")
        assert result.final_score == 0.8
        assert result.needs_review is False
        assert result.reviewed_by == "ben@ut.edu"
        assert result.review_note == "Partial credit for splitting."

    def test_override_preserves_suggested_for_audit(self):
        from axiom.extensions.builtins.classroom.quiz_scoring import (
            auto_score,
            override_score,
        )

        ft = auto_score(
            {"student_id": "s", "assessment_id": "q", "question_id": "FT",
             "question_type": "free_text", "answer": "x",
             "question": "What is fission?",
             "rubric": {"max_score": 1.0, "criteria": ["splitting"]}},
            answer_key={}, llm_grader=FakeLLMGrader(score=0.6),
        )
        result = override_score(ft, final=0.8, reviewer="i")
        assert result.suggested_score == 0.6


class TestAssessmentStats:
    def test_mean_median_distribution(self):
        from axiom.extensions.builtins.classroom.quiz_scoring import (
            ScoredResponse,
            assessment_stats,
        )

        scored = [
            ScoredResponse("s1", "pre", "Q1", "mcq", auto_score=1.0,
                           final_score=1.0, needs_review=False),
            ScoredResponse("s2", "pre", "Q1", "mcq", auto_score=0.0,
                           final_score=0.0, needs_review=False),
            ScoredResponse("s3", "pre", "Q1", "mcq", auto_score=1.0,
                           final_score=1.0, needs_review=False),
        ]
        stats = assessment_stats(scored, assessment_id="pre")
        assert stats["count"] == 3
        assert abs(stats["mean"] - 2/3) < 1e-9
        assert stats["median"] == 1.0
        assert stats["min"] == 0.0
        assert stats["max"] == 1.0

    def test_stats_filters_by_assessment(self):
        from axiom.extensions.builtins.classroom.quiz_scoring import (
            ScoredResponse,
            assessment_stats,
        )

        scored = [
            ScoredResponse("s1", "pre", "Q1", "mcq", final_score=1.0),
            ScoredResponse("s2", "post", "Q1", "mcq", final_score=0.0),
        ]
        assert assessment_stats(scored, assessment_id="pre")["count"] == 1

    def test_stats_excludes_queue_and_ungradable(self):
        from axiom.extensions.builtins.classroom.quiz_scoring import (
            ScoredResponse,
            assessment_stats,
        )

        scored = [
            ScoredResponse("s1", "q", "Q1", "mcq", final_score=1.0),
            ScoredResponse("s2", "q", "FT", "free_text", suggested_score=0.6,
                           needs_review=True),
            ScoredResponse("s3", "q", "L1", "likert"),
        ]
        stats = assessment_stats(scored, assessment_id="q")
        assert stats["count"] == 1


class TestPerStudentGrade:
    def test_aggregate_per_student(self):
        from axiom.extensions.builtins.classroom.quiz_scoring import (
            ScoredResponse,
            per_student_grades,
        )

        scored = [
            ScoredResponse("s1", "pre", "Q1", "mcq", final_score=1.0),
            ScoredResponse("s1", "pre", "Q2", "mcq", final_score=0.5),
            ScoredResponse("s2", "pre", "Q1", "mcq", final_score=0.0),
        ]
        grades = per_student_grades(scored, assessment_id="pre")
        assert grades["s1"]["score"] == 0.75
        assert grades["s1"]["questions"] == 2
        assert grades["s2"]["score"] == 0.0


class TestFederationSignedGrade:
    """Federation stretch: grade as cross-node signed claim (ADR-023).

    When scoring happens on a student's node, the grade can be
    serialized as a signed claim transported to the hub for
    aggregation. Signature slot is reserved for federation layer.
    """

    def test_serialize_grade_claim(self):
        from axiom.extensions.builtins.classroom.quiz_scoring import (
            ScoredResponse,
            serialize_grade_claim,
        )

        r = ScoredResponse("s1", "pre", "Q1", "mcq", final_score=0.75,
                           reviewed_by="ben@ut.edu")
        claim = serialize_grade_claim(r, signer_node="prague.axiom.eu")
        assert claim["student_id"] == "s1"
        assert claim["assessment_id"] == "pre"
        assert claim["final_score"] == 0.75
        assert claim["signer_node"] == "prague.axiom.eu"
        assert "signature" in claim  # reserved slot
