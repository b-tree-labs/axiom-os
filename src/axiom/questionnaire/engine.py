# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Structured Q&A engine — questionnaire delivery with branching and LLM wrapping.

Powers: onboarding interviews, assessments, check-ins, course review
instruments, and quiz-mode assessments (RAG-disabled).

The engine is stateless per-call: session state is passed in and
returned. Storage is the caller's responsibility. This makes it
testable without persistence and compatible with any backend (SQLite,
PostgreSQL, YAML file, in-memory).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Question:
    """Single question in a questionnaire."""

    id: str
    text: str
    type: str  # "free_text", "likert_scale", "yes_no"
    condition: str | None = None  # e.g. "Q1 == yes"
    scale: list | None = None  # [min, max] for likert
    anchors: list[str] | None = None  # human-readable anchors


@dataclass
class QASession:
    """Tracks a student's progress through one questionnaire."""

    student_id: str
    questionnaire_id: str
    status: str = "in_progress"  # "in_progress", "completed"
    current_question_index: int = 0
    responses: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class QAEngine:
    """Delivers a questionnaire with branching, typed responses, and LLM wrapping."""

    def __init__(self, manifest: dict) -> None:
        self.questionnaire_id: str = manifest.get("id", "")
        self.title: str = manifest.get("title", "")
        self.tone: str = manifest.get("tone", "neutral")
        self.allow_probes: bool = manifest.get("allow_probes", False)
        self.mode: str = manifest.get("mode", "interview")  # "interview" or "quiz"

        self.questions: list[Question] = [
            Question(
                id=q["id"],
                text=q["text"],
                type=q["type"],
                condition=q.get("condition"),
                scale=q.get("scale"),
                anchors=q.get("anchors"),
            )
            for q in manifest.get("questions", [])
        ]

    def start_session(self, student_id: str) -> QASession:
        """Create a new session for a student."""
        return QASession(
            student_id=student_id,
            questionnaire_id=self.questionnaire_id,
        )

    def get_current_question(self, session: QASession) -> Question | None:
        """Return the current question, respecting conditional branching.

        Advances past questions whose conditions are not met.
        Returns None if all questions are exhausted.
        """
        while session.current_question_index < len(self.questions):
            q = self.questions[session.current_question_index]
            if q.condition and not self._evaluate_condition(q.condition, session.responses):
                session.current_question_index += 1
                continue
            return q
        return None

    def submit_response(self, session: QASession, raw_answer: str) -> QASession:
        """Submit an answer for the current question and advance.

        Returns an updated session (new copy). Raises ValueError for
        invalid responses (e.g. likert out of range).
        """
        session = deepcopy(session)
        q = self.get_current_question(session)
        if q is None:
            return session  # already completed

        # Type-check and coerce the response
        parsed = self._parse_response(raw_answer, q)
        session.responses[q.id] = parsed
        session.current_question_index += 1

        # Check if there are more questions (skip conditionals)
        next_q = self.get_current_question(session)
        if next_q is None:
            session.status = "completed"

        return session

    def get_conversational_prompt(self, session: QASession) -> str:
        """Generate an LLM prompt that wraps the current question conversationally.

        The prompt includes tone guidance, question text, prior-answer context,
        and (for quiz mode) explicit instructions to disable assistance.
        """
        q = self.get_current_question(session)
        if q is None:
            return "The questionnaire is complete. Thank the student."

        parts: list[str] = []

        # Mode-specific framing
        if self.mode == "quiz":
            parts.append(
                "You are administering a quiz. Do NOT provide hints, answers, "
                "or assistance. Present the question clearly and record the "
                "student's answer exactly as given. Do not use RAG or external "
                "knowledge to help the student."
            )
        else:
            parts.append(
                f"You are conducting a {self.tone} {self.title}. "
                f"Present the following question in a natural, {self.tone} way. "
                "Make the student feel comfortable."
            )

        # Prior-answer context for natural flow
        if session.responses:
            last_q_id = list(session.responses.keys())[-1]
            last_answer = session.responses[last_q_id]
            parts.append(
                f'The student\'s previous answer was: "{last_answer}". '
                "Acknowledge it briefly before asking the next question."
            )

        # The actual question
        parts.append(f'Question to ask: "{q.text}"')

        # Type-specific guidance
        if q.type == "likert_scale" and q.scale:
            lo, hi = q.scale[0], q.scale[-1]
            anchor_text = ""
            if q.anchors and len(q.anchors) >= 2:
                anchor_text = f" ({q.anchors[0]} to {q.anchors[-1]})"
            parts.append(f"This is a rating question on a scale of {lo} to {hi}{anchor_text}.")
        elif q.type == "yes_no":
            parts.append("This is a yes/no question.")

        # Probe allowance
        if self.allow_probes and self.mode != "quiz":
            parts.append(
                "You may ask ONE brief follow-up probe if the student's answer "
                "is very short or unclear, but do not push."
            )

        return "\n".join(parts)

    # -- internals ----------------------------------------------------------

    def _parse_response(self, raw: str, question: Question) -> Any:
        """Parse and validate a raw response string by question type."""
        if question.type == "free_text":
            return raw.strip()

        if question.type == "yes_no":
            lowered = raw.strip().lower()
            if lowered in ("yes", "y", "true", "1"):
                return True
            if lowered in ("no", "n", "false", "0"):
                return False
            raise ValueError(f"Expected yes/no for {question.id}, got: {raw!r}")

        if question.type == "likert_scale":
            try:
                val = int(raw.strip())
            except ValueError:
                raise ValueError(
                    f"Expected integer for likert scale {question.id}, got: {raw!r}"
                ) from None
            if question.scale and len(question.scale) >= 2:
                lo, hi = question.scale[0], question.scale[-1]
                if val < lo or val > hi:
                    raise ValueError(f"Response {val} out of scale [{lo}, {hi}] for {question.id}")
            return val

        # Unknown type — store as string
        return raw.strip()

    def _evaluate_condition(self, condition: str, responses: dict[str, Any]) -> bool:
        """Evaluate a simple condition like 'Q1 == yes' against collected responses."""
        # Parse: "QID == value"
        parts = condition.strip().split("==")
        if len(parts) != 2:
            return True  # malformed condition → show the question

        q_id = parts[0].strip()
        expected = parts[1].strip().lower()

        actual = responses.get(q_id)
        if actual is None:
            return False  # question not yet answered → don't show conditional

        # Normalize for comparison
        if isinstance(actual, bool):
            return (expected in ("yes", "true", "1")) == actual
        return str(actual).lower() == expected
