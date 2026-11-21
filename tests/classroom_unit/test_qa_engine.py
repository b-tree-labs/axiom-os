# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for the Structured Q&A engine.

The engine delivers questionnaires conversationally via LLM wrapping,
handles branching conditions, captures typed responses, and tracks
completion. Powers: onboarding interviews, assessments, check-ins,
course review instruments.

Questionnaire manifests are YAML; the engine is format-agnostic
internally (works on dicts).
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# 1. MANIFEST LOADING
# ---------------------------------------------------------------------------


class TestManifestLoading:
    def test_load_basic_manifest(self):
        from axiom.extensions.builtins.classroom.qa_engine import QAEngine

        manifest = {
            "id": "begin-of-course-interview",
            "title": "Pre-Course Interview",
            "tone": "conversational",
            "allow_probes": True,
            "questions": [
                {"id": "Q1", "text": "How familiar are you with AI tools?", "type": "free_text"},
                {
                    "id": "Q2",
                    "text": "Comfort level with AI for learning?",
                    "type": "likert_scale",
                    "scale": [1, 5],
                },
                {"id": "Q3", "text": "Used AI in a previous course?", "type": "yes_no"},
            ],
        }
        engine = QAEngine(manifest)
        assert engine.questionnaire_id == "begin-of-course-interview"
        assert engine.title == "Pre-Course Interview"
        assert len(engine.questions) == 3
        assert engine.tone == "conversational"

    def test_load_with_conditional_questions(self):
        from axiom.extensions.builtins.classroom.qa_engine import QAEngine

        manifest = {
            "id": "test",
            "title": "Test",
            "questions": [
                {"id": "Q1", "text": "Do you have prior experience?", "type": "yes_no"},
                {
                    "id": "Q1a",
                    "text": "Describe your experience.",
                    "type": "free_text",
                    "condition": "Q1 == yes",
                },
                {"id": "Q2", "text": "What do you hope to learn?", "type": "free_text"},
            ],
        }
        engine = QAEngine(manifest)
        assert engine.questions[1].condition == "Q1 == yes"


# ---------------------------------------------------------------------------
# 2. SESSION MANAGEMENT
# ---------------------------------------------------------------------------


class TestSessionManagement:
    def test_start_session_for_student(self):
        from axiom.extensions.builtins.classroom.qa_engine import QAEngine

        engine = QAEngine(
            {
                "id": "interview",
                "title": "Interview",
                "questions": [{"id": "Q1", "text": "Hello?", "type": "free_text"}],
            }
        )
        session = engine.start_session(student_id="s1")
        assert session.student_id == "s1"
        assert session.questionnaire_id == "interview"
        assert session.status == "in_progress"
        assert session.current_question_index == 0
        assert len(session.responses) == 0

    def test_session_tracks_current_question(self):
        from axiom.extensions.builtins.classroom.qa_engine import QAEngine

        engine = QAEngine(
            {
                "id": "test",
                "title": "T",
                "questions": [
                    {"id": "Q1", "text": "First?", "type": "free_text"},
                    {"id": "Q2", "text": "Second?", "type": "free_text"},
                ],
            }
        )
        session = engine.start_session("s1")
        q = engine.get_current_question(session)
        assert q.id == "Q1"


# ---------------------------------------------------------------------------
# 3. RESPONSE CAPTURE + ADVANCEMENT
# ---------------------------------------------------------------------------


class TestResponseCapture:
    def _make_engine(self):
        from axiom.extensions.builtins.classroom.qa_engine import QAEngine

        return QAEngine(
            {
                "id": "test",
                "title": "T",
                "questions": [
                    {"id": "Q1", "text": "Name?", "type": "free_text"},
                    {"id": "Q2", "text": "Rating?", "type": "likert_scale", "scale": [1, 5]},
                    {"id": "Q3", "text": "Done?", "type": "yes_no"},
                ],
            }
        )

    def test_submit_free_text_response(self):
        engine = self._make_engine()
        session = engine.start_session("s1")

        session = engine.submit_response(session, "Alice")
        assert session.responses["Q1"] == "Alice"
        assert session.current_question_index == 1

    def test_submit_likert_response(self):
        engine = self._make_engine()
        session = engine.start_session("s1")
        session = engine.submit_response(session, "Alice")  # Q1
        session = engine.submit_response(session, "4")  # Q2 likert
        assert session.responses["Q2"] == 4  # stored as int

    def test_submit_yes_no_response(self):
        engine = self._make_engine()
        session = engine.start_session("s1")
        session = engine.submit_response(session, "Alice")  # Q1
        session = engine.submit_response(session, "4")  # Q2
        session = engine.submit_response(session, "yes")  # Q3
        assert session.responses["Q3"] is True
        assert session.status == "completed"

    def test_likert_out_of_range_rejected(self):
        engine = self._make_engine()
        session = engine.start_session("s1")
        session = engine.submit_response(session, "Alice")  # Q1

        with pytest.raises(ValueError, match="scale"):
            engine.submit_response(session, "10")  # out of range

    def test_session_completes_after_last_question(self):
        engine = self._make_engine()
        session = engine.start_session("s1")
        session = engine.submit_response(session, "Alice")
        session = engine.submit_response(session, "3")
        session = engine.submit_response(session, "no")
        assert session.status == "completed"
        assert len(session.responses) == 3


# ---------------------------------------------------------------------------
# 4. CONDITIONAL BRANCHING
# ---------------------------------------------------------------------------


class TestConditionalBranching:
    def test_condition_met_shows_question(self):
        from axiom.extensions.builtins.classroom.qa_engine import QAEngine

        engine = QAEngine(
            {
                "id": "test",
                "title": "T",
                "questions": [
                    {"id": "Q1", "text": "Prior experience?", "type": "yes_no"},
                    {
                        "id": "Q1a",
                        "text": "Describe it.",
                        "type": "free_text",
                        "condition": "Q1 == yes",
                    },
                    {"id": "Q2", "text": "Goals?", "type": "free_text"},
                ],
            }
        )
        session = engine.start_session("s1")
        session = engine.submit_response(session, "yes")  # Q1 = yes → Q1a shown

        q = engine.get_current_question(session)
        assert q.id == "Q1a"

    def test_condition_not_met_skips_question(self):
        from axiom.extensions.builtins.classroom.qa_engine import QAEngine

        engine = QAEngine(
            {
                "id": "test",
                "title": "T",
                "questions": [
                    {"id": "Q1", "text": "Prior experience?", "type": "yes_no"},
                    {
                        "id": "Q1a",
                        "text": "Describe it.",
                        "type": "free_text",
                        "condition": "Q1 == yes",
                    },
                    {"id": "Q2", "text": "Goals?", "type": "free_text"},
                ],
            }
        )
        session = engine.start_session("s1")
        session = engine.submit_response(session, "no")  # Q1 = no → skip Q1a

        q = engine.get_current_question(session)
        assert q.id == "Q2"  # skipped Q1a

    def test_skipped_questions_not_in_responses(self):
        from axiom.extensions.builtins.classroom.qa_engine import QAEngine

        engine = QAEngine(
            {
                "id": "test",
                "title": "T",
                "questions": [
                    {"id": "Q1", "text": "Prior?", "type": "yes_no"},
                    {
                        "id": "Q1a",
                        "text": "Details.",
                        "type": "free_text",
                        "condition": "Q1 == yes",
                    },
                    {"id": "Q2", "text": "Goals?", "type": "free_text"},
                ],
            }
        )
        session = engine.start_session("s1")
        session = engine.submit_response(session, "no")  # skip Q1a
        session = engine.submit_response(session, "Learn stuff")  # Q2

        assert "Q1a" not in session.responses
        assert session.status == "completed"


# ---------------------------------------------------------------------------
# 5. LLM CONVERSATIONAL WRAPPING
# ---------------------------------------------------------------------------


class TestConversationalWrapping:
    def test_get_prompt_wraps_question(self):
        from axiom.extensions.builtins.classroom.qa_engine import QAEngine

        engine = QAEngine(
            {
                "id": "test",
                "title": "Pre-Course Interview",
                "tone": "conversational",
                "questions": [
                    {
                        "id": "Q1",
                        "text": "How familiar are you with AI tools?",
                        "type": "free_text",
                    },
                ],
            }
        )
        session = engine.start_session("s1")
        prompt = engine.get_conversational_prompt(session)

        # The prompt should include the question text and tone guidance
        assert "AI tools" in prompt
        assert "conversational" in prompt.lower() or "natural" in prompt.lower()

    def test_prompt_includes_context_for_followup(self):
        from axiom.extensions.builtins.classroom.qa_engine import QAEngine

        engine = QAEngine(
            {
                "id": "test",
                "title": "Interview",
                "tone": "conversational",
                "allow_probes": True,
                "questions": [
                    {"id": "Q1", "text": "Experience with AI?", "type": "free_text"},
                    {"id": "Q2", "text": "Comfort level?", "type": "likert_scale", "scale": [1, 5]},
                ],
            }
        )
        session = engine.start_session("s1")
        session = engine.submit_response(session, "I use ChatGPT daily")

        prompt = engine.get_conversational_prompt(session)
        # Should reference prior answer context for natural flow
        assert "Comfort level" in prompt or "comfort" in prompt.lower()


# ---------------------------------------------------------------------------
# 6. QUIZ MODE (RAG-disabled assessment)
# ---------------------------------------------------------------------------


class TestQuizMode:
    def test_quiz_mode_flag(self):
        from axiom.extensions.builtins.classroom.qa_engine import QAEngine

        engine = QAEngine(
            {
                "id": "midterm",
                "title": "Mid-Course Quiz",
                "mode": "quiz",
                "questions": [
                    {"id": "Q1", "text": "What is fission?", "type": "free_text"},
                ],
            }
        )
        assert engine.mode == "quiz"

    def test_quiz_mode_prompt_disables_assistance(self):
        from axiom.extensions.builtins.classroom.qa_engine import QAEngine

        engine = QAEngine(
            {
                "id": "midterm",
                "title": "Quiz",
                "mode": "quiz",
                "questions": [
                    {"id": "Q1", "text": "Define chain reaction.", "type": "free_text"},
                ],
            }
        )
        session = engine.start_session("s1")
        prompt = engine.get_conversational_prompt(session)

        # Quiz mode should explicitly disable RAG/help
        assert (
            "do not" in prompt.lower()
            or "no assistance" in prompt.lower()
            or "quiz" in prompt.lower()
        )
