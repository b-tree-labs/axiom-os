# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ClassroomPrepWorkflow — orchestrator for classroom instantiation.

Wraps the pure classroom_prep state machine (4 instance steps) with
executors that perform each step against injectable backends (LLM,
retriever, LMS). The classroom references a published course.

Flow:
    1. Select course (new course_prep flow, or pick existing published)
    2. Select RAG policy (course_only / institutional / A/B / etc.)
    3. Connect LMS (Canvas) and preview roster
    4. Run dry-run (instructor as test student) — recommended

Once critical steps are green, `axi classroom create` can proceed
to enrollment.

Spec: spec-classroom.md §2.6.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from .classroom_prep import (
    ClassroomPrepChecklist,
    check_classroom_ready_for_enrollment,
    create_classroom_prep_checklist,
    validate_course_selected_step,
    validate_dry_run_step,
    validate_lms_step,
    validate_rag_policy_step,
)

# ---------------------------------------------------------------------------
# Backend protocols
# ---------------------------------------------------------------------------


class CorpusRetriever(Protocol):
    def retrieve(self, query: str, k: int = 5) -> list[dict]: ...


LLMBackend = Callable[..., str]


class LMSProvider(Protocol):
    def ping(self) -> bool: ...
    def list_students(self, course_id: str) -> list[dict]: ...


_VALID_RAG_MODES = {"course_only", "course_plus_institutional", "full", "ab_test", "custom"}

_RAG_POLICY_NAMES = {
    "course_only": "Course Materials Only",
    "course_plus_institutional": "Course + Institutional",
    "full": "Course + Institutional + Community",
    "ab_test": "A/B Test",
    "custom": "Custom",
}


# ---------------------------------------------------------------------------
# Dry-run result
# ---------------------------------------------------------------------------


@dataclass
class DryRunResult:
    turns: int
    transcript: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@dataclass
class ClassroomPrepWorkflow:
    """Drives the 4-step classroom instantiation flow."""

    instructor_id: str
    classroom_id: str
    retriever: CorpusRetriever
    llm: LLMBackend
    lms: LMSProvider

    # Classroom-scoped state
    course_id: str | None = None
    course_version: str | None = None
    course_system_prompt: str | None = None
    rag_policy_mode: str | None = None
    lms_roster: list[dict] = field(default_factory=list)

    checklist: ClassroomPrepChecklist = field(init=False)

    def __post_init__(self) -> None:
        self.checklist = create_classroom_prep_checklist(
            self.instructor_id, self.classroom_id
        )

    # --- Step 1: Course selection ------------------------------------------

    def select_course(
        self,
        course_id: str,
        course_version: str,
        publishable: bool,
        system_prompt: str | None = None,
    ) -> None:
        """Select a published (or prepped) course for this classroom.

        The caller is responsible for determining `publishable` via
        course_prep's is_ready_to_publish; we just record the decision.
        """
        self.course_id = course_id
        self.course_version = course_version
        self.course_system_prompt = system_prompt
        self.checklist = validate_course_selected_step(
            self.checklist,
            course_id=course_id,
            course_version=course_version,
            publishable=publishable,
        )

    # --- Step 2: RAG policy -------------------------------------------------

    def select_rag_policy(self, mode: str) -> None:
        if mode not in _VALID_RAG_MODES:
            raise ValueError(
                f"unknown RAG policy mode: {mode!r}; "
                f"expected one of {sorted(_VALID_RAG_MODES)}"
            )
        self.rag_policy_mode = mode
        self.checklist = validate_rag_policy_step(
            self.checklist,
            policy_id=f"{self.classroom_id}-{mode}",
            policy_name=_RAG_POLICY_NAMES[mode],
        )

    # --- Step 3: LMS connection --------------------------------------------

    def connect_lms(self, course_id: str) -> list[dict]:
        connected = self.lms.ping()
        roster: list[dict] = []
        if connected:
            roster = self.lms.list_students(course_id)
        self.lms_roster = roster
        self.checklist = validate_lms_step(
            self.checklist,
            lms_connected=connected,
            roster_count=len(roster),
        )
        return roster

    # --- Step 4: Dry run ----------------------------------------------------

    def run_dry_run(self, sample_queries: list[str]) -> DryRunResult:
        if self.course_system_prompt is None:
            raise RuntimeError(
                "Dry run requires a course system prompt; "
                "select a publishable course first via select_course()"
            )
        if self.checklist.steps[0].status != "completed":
            raise RuntimeError(
                "Dry run requires a selected course; "
                "call select_course() first"
            )

        transcript: list[dict] = []
        for query in sample_queries:
            retrieved = self.retriever.retrieve(query)
            rag_context = "\n\n".join(r.get("text", "") for r in retrieved)
            messages = [
                {"role": "system", "content": self.course_system_prompt},
            ]
            if rag_context:
                messages.append(
                    {"role": "system", "content": f"Retrieved context:\n{rag_context}"}
                )
            messages.append({"role": "user", "content": query})
            response = self.llm(messages)
            transcript.append(
                {"query": query, "response": response, "retrieved": retrieved}
            )

        self.checklist = validate_dry_run_step(self.checklist, dry_run_completed=True)
        return DryRunResult(turns=len(sample_queries), transcript=transcript)

    # --- Readiness + summary -----------------------------------------------

    def is_ready_for_enrollment(self) -> tuple[bool, list[str]]:
        return check_classroom_ready_for_enrollment(self.checklist)

    def summary(self) -> dict:
        ready, blockers = self.is_ready_for_enrollment()
        return {
            "instructor_id": self.instructor_id,
            "classroom_id": self.classroom_id,
            "course_id": self.course_id,
            "course_version": self.course_version,
            "ready": ready,
            "blockers": blockers,
            "steps": [
                {
                    "name": s.name,
                    "status": s.status,
                    "critical": s.critical,
                    "message": s.message,
                    "description": s.description,
                }
                for s in self.checklist.steps
            ],
        }
