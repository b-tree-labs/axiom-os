# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Instructor-broadcast quizzes — "push a quiz to everyone" primitive.

Tier C4. The instructor presses a button (CLI for now, web tab later)
and N questions land as a quiz in every student's pending list.
Students take it when they next run ``axi classroom quiz take``,
answers post back to the coordinator, scoring reuses the keyword
scorer from the evals framework.

Storage layout under ``<base_dir>``::

    <base_dir>/quizzes/<quiz_id>.json
    <base_dir>/quiz_submissions/<quiz_id>/<safe_student_id>.json

One JSON per quiz keeps listing cheap; one submission per file keeps
writes concurrent-safe and makes "who hasn't submitted" a trivial
directory listing.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .classroom_evals import KeywordScore, score_keywords

# ---------------------------------------------------------------------------
# IDs
# ---------------------------------------------------------------------------


def new_quiz_id() -> str:
    """Short url-safe quiz id.

    Excludes leading '-' and '_' so the id is always argparse-safe as a
    positional arg (otherwise argparse mis-parses dash-leading positionals
    as unknown options and reports the positional as missing — see
    `classroom_threads._new_thread_id` for the same fix). The 2026-05-04
    flake on `test_taken_quiz_disappears_from_pending` (Unit Tests 3.13)
    was a recurrence of this class of bug.
    """
    while True:
        qid = secrets.token_urlsafe(9)  # ~12 chars
        if qid[0] not in ("-", "_"):
            return qid


def _safe_student_id(sid: str) -> str:
    """Filename-safe student id. Keeps @ → _ so alice@ut.edu is readable."""
    return sid.replace("/", "_").replace("\\", "_").replace("@", "_at_")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuizQuestion:
    question_text: str
    expected_keywords: list[str]
    category: str | None = None


@dataclass(frozen=True)
class BroadcastedQuiz:
    quiz_id: str
    classroom_id: str
    created_at: str  # ISO 8601 with tz
    created_by: str  # instructor handle
    topic: str | None
    questions: list[QuizQuestion] = field(default_factory=list)


@dataclass(frozen=True)
class QuizAnswer:
    question_index: int
    answer_text: str


@dataclass(frozen=True)
class QuizSubmission:
    quiz_id: str
    student_id: str
    submitted_at: str
    answers: list[QuizAnswer] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@dataclass
class QuizStore:
    base_dir: Path

    # ---- Paths ----

    @property
    def _quizzes_dir(self) -> Path:
        return self.base_dir / "quizzes"

    @property
    def _submissions_dir(self) -> Path:
        return self.base_dir / "quiz_submissions"

    def _quiz_path(self, quiz_id: str) -> Path:
        return self._quizzes_dir / f"{quiz_id}.json"

    def _submission_path(self, quiz_id: str, student_id: str) -> Path:
        return (
            self._submissions_dir / quiz_id
            / f"{_safe_student_id(student_id)}.json"
        )

    # ---- Quizzes ----

    def save(self, quiz: BroadcastedQuiz) -> None:
        self._quizzes_dir.mkdir(parents=True, exist_ok=True)
        self._quiz_path(quiz.quiz_id).write_text(
            json.dumps(_quiz_to_dict(quiz), indent=2)
        )

    def get(self, quiz_id: str) -> BroadcastedQuiz | None:
        path = self._quiz_path(quiz_id)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
        return _quiz_from_dict(raw)

    def list_all(self) -> list[BroadcastedQuiz]:
        if not self._quizzes_dir.is_dir():
            return []
        out: list[BroadcastedQuiz] = []
        for p in self._quizzes_dir.glob("*.json"):
            try:
                out.append(_quiz_from_dict(json.loads(p.read_text())))
            except (json.JSONDecodeError, KeyError):
                continue
        out.sort(key=lambda q: q.created_at, reverse=True)
        return out

    # ---- Submissions ----

    def save_submission(self, sub: QuizSubmission) -> None:
        path = self._submission_path(sub.quiz_id, sub.student_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_submission_to_dict(sub), indent=2))

    def get_submission(
        self, quiz_id: str, student_id: str,
    ) -> QuizSubmission | None:
        path = self._submission_path(quiz_id, student_id)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
        return _submission_from_dict(raw)

    def has_submitted(self, quiz_id: str, student_id: str) -> bool:
        return self._submission_path(quiz_id, student_id).is_file()

    def pending_for_student(self, student_id: str) -> list[BroadcastedQuiz]:
        return [
            q for q in self.list_all()
            if not self.has_submitted(q.quiz_id, student_id)
        ]

    def submissions_for_quiz(self, quiz_id: str) -> list[QuizSubmission]:
        d = self._submissions_dir / quiz_id
        if not d.is_dir():
            return []
        out: list[QuizSubmission] = []
        for p in d.glob("*.json"):
            try:
                out.append(_submission_from_dict(json.loads(p.read_text())))
            except (json.JSONDecodeError, KeyError):
                continue
        return out


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_submission(
    quiz: BroadcastedQuiz,
    sub: QuizSubmission,
) -> tuple[list[KeywordScore], float]:
    """Score every question in the quiz against the submission.

    Returns ``(per_question_scores, overall_pass_rate)``. Missing
    answers count as fails — students are responsible for answering
    every question or accepting the hit.
    """
    answers_by_idx = {a.question_index: a.answer_text for a in sub.answers}
    per_q: list[KeywordScore] = []
    for i, q in enumerate(quiz.questions):
        per_q.append(score_keywords(
            answer=answers_by_idx.get(i, ""),
            expected_keywords=q.expected_keywords,
        ))
    passed = sum(1 for s in per_q if s.passed)
    rate = passed / len(per_q) if per_q else 0.0
    return per_q, rate


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _quiz_to_dict(quiz: BroadcastedQuiz) -> dict:
    return {
        "quiz_id": quiz.quiz_id,
        "classroom_id": quiz.classroom_id,
        "created_at": quiz.created_at,
        "created_by": quiz.created_by,
        "topic": quiz.topic,
        "questions": [asdict(q) for q in quiz.questions],
    }


def _quiz_from_dict(raw: dict) -> BroadcastedQuiz:
    questions = [
        QuizQuestion(
            question_text=str(q["question_text"]),
            expected_keywords=list(q.get("expected_keywords") or []),
            category=q.get("category"),
        )
        for q in raw.get("questions", [])
    ]
    return BroadcastedQuiz(
        quiz_id=str(raw["quiz_id"]),
        classroom_id=str(raw["classroom_id"]),
        created_at=str(raw["created_at"]),
        created_by=str(raw["created_by"]),
        topic=raw.get("topic"),
        questions=questions,
    )


def _submission_to_dict(sub: QuizSubmission) -> dict:
    return {
        "quiz_id": sub.quiz_id,
        "student_id": sub.student_id,
        "submitted_at": sub.submitted_at,
        "answers": [asdict(a) for a in sub.answers],
    }


def _submission_from_dict(raw: dict) -> QuizSubmission:
    answers = [
        QuizAnswer(
            question_index=int(a["question_index"]),
            answer_text=str(a["answer_text"]),
        )
        for a in raw.get("answers", [])
    ]
    return QuizSubmission(
        quiz_id=str(raw["quiz_id"]),
        student_id=str(raw["student_id"]),
        submitted_at=str(raw["submitted_at"]),
        answers=answers,
    )


__all__ = [
    "BroadcastedQuiz",
    "QuizAnswer",
    "QuizQuestion",
    "QuizStore",
    "QuizSubmission",
    "new_quiz_id",
    "score_submission",
]
