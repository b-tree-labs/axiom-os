# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the broadcast-quiz data model + store.

An instructor creates a quiz ("3 questions on control rods, push to
everyone"), students pull pending quizzes, take them interactively,
submit answers back. Scoring reuses the keyword scorer from the
evals framework for consistency.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.classroom.broadcast_quizzes import (
    BroadcastedQuiz,
    QuizAnswer,
    QuizQuestion,
    QuizStore,
    QuizSubmission,
    new_quiz_id,
    score_submission,
)

# ---------------------------------------------------------------------------
# IDs + shape
# ---------------------------------------------------------------------------


class TestIds:
    def test_new_id_is_unique(self):
        assert len({new_quiz_id() for _ in range(100)}) == 100

    def test_new_id_is_url_safe(self):
        for _ in range(20):
            qid = new_quiz_id()
            assert all(c.isalnum() or c in "_-" for c in qid)

    def test_new_id_never_starts_with_dash_or_underscore(self):
        """Regression: a quiz_id starting with '-' or '_' is mis-parsed by
        argparse as an option flag when the id is passed as a positional
        CLI arg, producing a misleading "the following arguments are
        required" failure. Recurrence: 2026-05-04 flake on
        test_taken_quiz_disappears_from_pending (Unit Tests 3.13). Mirrors
        the same fix already in classroom_threads._new_thread_id.

        1000 samples puts the false-pass probability under 1e-7 if
        the leading-char distribution were uniform over all 64 base64
        chars (2/64 ≈ 3.1% per draw)."""
        for _ in range(1000):
            qid = new_quiz_id()
            assert qid[0] not in ("-", "_"), (
                f"quiz_id {qid!r} starts with '{qid[0]}' — argparse will "
                "mis-parse as an option flag when used as a positional "
                "CLI argument"
            )


class TestShape:
    def test_quiz_is_frozen(self):
        q = BroadcastedQuiz(
            quiz_id="q", classroom_id="c", created_at="t",
            created_by="i", topic=None, questions=[],
        )
        with pytest.raises((AttributeError, Exception)):
            q.topic = "x"

    def test_question_is_frozen(self):
        q = QuizQuestion(question_text="Q?", expected_keywords=["k"])
        with pytest.raises((AttributeError, Exception)):
            q.question_text = "changed"


# ---------------------------------------------------------------------------
# Store — save + load quizzes
# ---------------------------------------------------------------------------


class TestQuizStore:
    def test_save_and_get_quiz(self, tmp_path):
        store = QuizStore(tmp_path)
        quiz = BroadcastedQuiz(
            quiz_id="q1", classroom_id="NE101",
            created_at="2026-04-23T10:00+00:00",
            created_by="@prof:ut",
            topic="control rods",
            questions=[
                QuizQuestion(
                    question_text="What is a control rod?",
                    expected_keywords=["absorb", "neutron"],
                ),
            ],
        )
        store.save(quiz)
        loaded = store.get("q1")
        assert loaded is not None
        assert loaded.quiz_id == "q1"
        assert len(loaded.questions) == 1
        assert loaded.questions[0].expected_keywords == ["absorb", "neutron"]

    def test_get_unknown_returns_none(self, tmp_path):
        store = QuizStore(tmp_path)
        assert store.get("never-seen") is None

    def test_list_all_newest_first(self, tmp_path):
        store = QuizStore(tmp_path)
        for ts in ["2026-04-21", "2026-04-23", "2026-04-22"]:
            store.save(BroadcastedQuiz(
                quiz_id=f"q_{ts}", classroom_id="NE101",
                created_at=f"{ts}T10:00+00:00",
                created_by="@prof:ut", topic=None, questions=[],
            ))
        listed = store.list_all()
        assert [q.quiz_id for q in listed] == [
            "q_2026-04-23", "q_2026-04-22", "q_2026-04-21",
        ]


# ---------------------------------------------------------------------------
# Submissions — per-student, per-quiz
# ---------------------------------------------------------------------------


class TestSubmissions:
    def test_save_and_get_submission(self, tmp_path):
        store = QuizStore(tmp_path)
        store.save(BroadcastedQuiz(
            quiz_id="q1", classroom_id="NE101",
            created_at="2026-04-23T10:00+00:00", created_by="i",
            topic=None,
            questions=[
                QuizQuestion(question_text="Q1?", expected_keywords=["k1"]),
            ],
        ))
        submission = QuizSubmission(
            quiz_id="q1", student_id="alice@ut.edu",
            submitted_at="2026-04-23T10:05+00:00",
            answers=[QuizAnswer(question_index=0, answer_text="the answer")],
        )
        store.save_submission(submission)
        loaded = store.get_submission("q1", "alice@ut.edu")
        assert loaded is not None
        assert loaded.answers[0].answer_text == "the answer"

    def test_has_submitted_returns_true_after_save(self, tmp_path):
        store = QuizStore(tmp_path)
        assert store.has_submitted("q1", "alice@ut.edu") is False
        store.save_submission(QuizSubmission(
            quiz_id="q1", student_id="alice@ut.edu",
            submitted_at="t", answers=[],
        ))
        assert store.has_submitted("q1", "alice@ut.edu") is True

    def test_pending_for_student_excludes_submitted(self, tmp_path):
        store = QuizStore(tmp_path)
        for qid in ("q1", "q2", "q3"):
            store.save(BroadcastedQuiz(
                quiz_id=qid, classroom_id="NE101",
                created_at=f"2026-04-23T10:0{qid[-1]}+00:00",
                created_by="i", topic=None,
                questions=[QuizQuestion(
                    question_text="Q", expected_keywords=["k"],
                )],
            ))
        # Alice has submitted q1 only.
        store.save_submission(QuizSubmission(
            quiz_id="q1", student_id="alice@ut.edu",
            submitted_at="t", answers=[],
        ))
        pending = store.pending_for_student("alice@ut.edu")
        assert {q.quiz_id for q in pending} == {"q2", "q3"}

    def test_all_submissions_for_quiz(self, tmp_path):
        store = QuizStore(tmp_path)
        for sid in ("alice", "bob", "carol"):
            store.save_submission(QuizSubmission(
                quiz_id="q1", student_id=sid,
                submitted_at="t", answers=[],
            ))
        subs = store.submissions_for_quiz("q1")
        assert {s.student_id for s in subs} == {"alice", "bob", "carol"}


# ---------------------------------------------------------------------------
# Scoring — reuse keyword scorer from evals
# ---------------------------------------------------------------------------


class TestScoring:
    def test_all_correct(self):
        quiz = BroadcastedQuiz(
            quiz_id="q", classroom_id="c", created_at="t",
            created_by="i", topic=None,
            questions=[
                QuizQuestion(question_text="Q1", expected_keywords=["absorb"]),
                QuizQuestion(question_text="Q2", expected_keywords=["neutron"]),
            ],
        )
        sub = QuizSubmission(
            quiz_id="q", student_id="alice", submitted_at="t",
            answers=[
                QuizAnswer(question_index=0, answer_text="It absorbs them"),
                QuizAnswer(question_index=1, answer_text="A neutron is released"),
            ],
        )
        per_q, total = score_submission(quiz, sub)
        assert [s.passed for s in per_q] == [True, True]
        assert total == 1.0  # 2/2

    def test_partial(self):
        quiz = BroadcastedQuiz(
            quiz_id="q", classroom_id="c", created_at="t",
            created_by="i", topic=None,
            questions=[
                QuizQuestion(question_text="Q1", expected_keywords=["absorb"]),
                QuizQuestion(question_text="Q2", expected_keywords=["neutron"]),
                QuizQuestion(question_text="Q3", expected_keywords=["water"]),
            ],
        )
        sub = QuizSubmission(
            quiz_id="q", student_id="alice", submitted_at="t",
            answers=[
                QuizAnswer(question_index=0, answer_text="It absorbs them"),
                QuizAnswer(question_index=1, answer_text="not sure"),
                QuizAnswer(question_index=2, answer_text="it uses water as coolant"),
            ],
        )
        per_q, total = score_submission(quiz, sub)
        assert [s.passed for s in per_q] == [True, False, True]
        assert abs(total - 2/3) < 1e-9

    def test_missing_answers_scored_as_fail(self):
        quiz = BroadcastedQuiz(
            quiz_id="q", classroom_id="c", created_at="t",
            created_by="i", topic=None,
            questions=[
                QuizQuestion(question_text="Q1", expected_keywords=["absorb"]),
                QuizQuestion(question_text="Q2", expected_keywords=["neutron"]),
            ],
        )
        # Student only answered question 0.
        sub = QuizSubmission(
            quiz_id="q", student_id="alice", submitted_at="t",
            answers=[
                QuizAnswer(question_index=0, answer_text="it absorbs"),
            ],
        )
        per_q, total = score_submission(quiz, sub)
        assert [s.passed for s in per_q] == [True, False]
        assert total == 0.5


# ---------------------------------------------------------------------------
# Disk layout — locked so HTTP endpoints can rely on it
# ---------------------------------------------------------------------------


class TestDiskLayout:
    def test_quiz_lives_at_quizzes_subdir(self, tmp_path):
        store = QuizStore(tmp_path)
        store.save(BroadcastedQuiz(
            quiz_id="q1", classroom_id="c",
            created_at="t", created_by="i", topic=None, questions=[],
        ))
        path = tmp_path / "quizzes" / "q1.json"
        assert path.is_file()

    def test_submission_lives_under_submissions_subdir(self, tmp_path):
        store = QuizStore(tmp_path)
        store.save_submission(QuizSubmission(
            quiz_id="q1", student_id="alice@ut.edu",
            submitted_at="t", answers=[],
        ))
        # Student IDs may contain @/. — those should be sanitized.
        sub_dir = tmp_path / "quiz_submissions" / "q1"
        assert sub_dir.is_dir()
        assert any(p.is_file() for p in sub_dir.glob("*.json"))
