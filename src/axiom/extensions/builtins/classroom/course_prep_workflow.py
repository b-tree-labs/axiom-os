# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CoursePrepWorkflow — instructor-facing orchestrator for course authoring.

Wraps the pure course_prep state machine (5 template steps) with
executors that perform each step against injectable backends
(corpus indexer, retriever, LLM). Produces a publishable course
artifact. Classroom instantiation is a separate flow —
see `classroom_prep_workflow.py`.

Spec: spec-classroom.md §2.6/§2.7.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .course_prep import (
    CoursePrepChecklist,
    check_course_ready_to_publish,
    create_prep_checklist,
    validate_assessment_step,
    validate_corpus_step,
    validate_manifest_step,
    validate_prompt_step,
    validate_rails_step,
)

# ---------------------------------------------------------------------------
# Backend protocols
# ---------------------------------------------------------------------------


class CorpusIndexer(Protocol):
    def index(self, documents: list[dict]) -> int: ...


class CorpusRetriever(Protocol):
    def retrieve(self, query: str, k: int = 5) -> list[dict]: ...


LLMBackend = Callable[..., str]


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@dataclass
class CoursePrepWorkflow:
    """Drives the 5-step course template authoring flow.

    Not concerned with roster/LMS/RAG policy — those belong to the
    Classroom (instance) layer. See `ClassroomPrepWorkflow`.
    """

    instructor_id: str
    course_id: str
    indexer: CorpusIndexer
    retriever: CorpusRetriever
    llm: LLMBackend

    # Authored state
    manifest: dict | None = None
    system_prompt: str | None = None
    _last_prompt_test: dict | None = None
    assessments: list[dict] = field(default_factory=list)
    rails: list[dict] = field(default_factory=list)
    corpus_doc_count: int = 0
    last_preview: list[dict] = field(default_factory=list)

    checklist: CoursePrepChecklist = field(init=False)

    def __post_init__(self) -> None:
        self.checklist = create_prep_checklist(self.instructor_id, self.course_id)

    # --- Step 1: Manifest ---------------------------------------------------

    def load_manifest(self, manifest: dict[str, Any]) -> None:
        self.manifest = manifest
        self.checklist = validate_manifest_step(self.checklist, manifest)

    # --- Step 2: Corpus -----------------------------------------------------

    def upload_corpus(self, documents: list[dict]) -> int:
        self.corpus_doc_count = self.indexer.index(documents)
        return self.corpus_doc_count

    def preview_corpus(self, query: str) -> list[dict]:
        results = self.retriever.retrieve(query)
        self.last_preview = results
        self.checklist = validate_corpus_step(
            self.checklist,
            corpus_doc_count=self.corpus_doc_count,
            test_query=query,
            test_results=results,
        )
        return results

    # --- Step 3: System prompt ---------------------------------------------

    def test_prompt(self, system_prompt: str, test_query: str) -> str:
        self.system_prompt = system_prompt
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": test_query},
        ]
        response = self.llm(messages)
        self._last_prompt_test = {
            "system_prompt": system_prompt,
            "query": test_query,
            "response": response,
        }
        self.checklist = validate_prompt_step(
            self.checklist,
            system_prompt=system_prompt,
            test_response=response,
            instructor_approved=False,
        )
        return response

    def approve_prompt(self) -> None:
        if self._last_prompt_test is None:
            raise RuntimeError(
                "No test prompt to approve — call test_prompt() first"
            )
        self.checklist = validate_prompt_step(
            self.checklist,
            system_prompt=self._last_prompt_test["system_prompt"],
            test_response=self._last_prompt_test["response"],
            instructor_approved=True,
        )

    # --- Step 4: Assessments -----------------------------------------------

    def define_assessment(self, assessment: dict) -> None:
        self.assessments.append(assessment)
        self.checklist = validate_assessment_step(self.checklist, len(self.assessments))

    def skip_assessments(self) -> None:
        self.checklist = validate_assessment_step(self.checklist, 0)

    # --- Step 5: Onboarding rails ------------------------------------------

    def configure_rails(self, rails: list[dict]) -> None:
        self.rails = list(rails)
        self.checklist = validate_rails_step(self.checklist, len(self.rails))

    def use_default_rails(self) -> None:
        self.checklist = validate_rails_step(self.checklist, 0)

    # --- Publish readiness --------------------------------------------------

    def is_ready_to_publish(self) -> tuple[bool, list[str]]:
        return check_course_ready_to_publish(self.checklist)

    def summary(self) -> dict:
        ready, blockers = self.is_ready_to_publish()
        return {
            "instructor_id": self.instructor_id,
            "course_id": self.course_id,
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
