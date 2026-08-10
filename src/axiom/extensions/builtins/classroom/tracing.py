# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom tracing — per-student attribution + composition integration.

Wraps the existing TraceProvider (LangFuse, InMemory, or Null) with
classroom-specific metadata. Every trace is also materialized as a
MemoryFragment(episodic) through CompositionService when one is
available — this is the #71 migration that makes traces a first-class
part of the unified memory stack.

Two storage paths coexist cleanly:
- Underlying TraceProvider: real-time trace events (LLM calls,
  retrievals, scores) for observability backends like LangFuse.
- MemoryFragment(episodic) via CompositionService: durable,
  ownership-bearing, signed, audit-logged record of each session start.

Both end up with the same `trace_id`. Analytics queries (who asked
what, when) can use the fragment side; real-time LLM observability
uses the provider side.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from axiom.infra.tracing import NullTraceProvider

if TYPE_CHECKING:
    from axiom.memory.composition import CompositionService
    from axiom.memory.fragment import MemoryFragment


class ClassroomTracer:
    """Classroom-attributed trace wrapper with composition integration."""

    def __init__(
        self,
        classroom_id: str,
        course_id: str,
        trace_provider: Any = None,
        composition: CompositionService | None = None,
    ) -> None:
        self.classroom_id = classroom_id
        self.course_id = course_id
        self._provider = trace_provider or NullTraceProvider()
        self._composition = composition
        # Fast-lookup cache; authoritative store is the composition registry
        self._student_traces: dict[str, list[str]] = {}
        # trace_id → fragment_id so analytics can round-trip
        self._trace_to_fragment: dict[str, str] = {}

    def _base_metadata(self, student_id: str, session_type: str, **extra: Any) -> dict:
        meta = {
            "student_id": student_id,
            "classroom_id": self.classroom_id,
            "course_id": self.course_id,
            "session_type": session_type,
        }
        meta.update(extra)
        return meta

    def _record(self, student_id: str, trace_id: str) -> None:
        self._student_traces.setdefault(student_id, []).append(trace_id)

    def _write_fragment(
        self,
        *,
        student_id: str,
        session_type: str,
        trace_id: str,
        payload: dict,
    ) -> None:
        """Materialize an episodic fragment for this trace event.

        No-op when `composition` wasn't wired — preserves the legacy
        behavior for callers that haven't adopted the unified stack yet.
        """
        if self._composition is None:
            return
        fragment = self._composition.write(
            content={
                "event_time": datetime.now(UTC).isoformat(),
                "trace_id": trace_id,
                "session_type": session_type,
                "classroom_id": self.classroom_id,
                "course_id": self.course_id,
                **payload,
            },
            cognitive_type="episodic",
            principal_id=student_id,
            agents={"axi"},
            resources={f"classroom:{self.classroom_id}"},
        )
        self._trace_to_fragment[trace_id] = fragment.id

    # -- trace creation by session type ------------------------------------

    def trace_chat(self, student_id: str, message: str) -> str:
        meta = self._base_metadata(student_id, "chat")
        trace_id = self._provider.start_trace(f"classroom.chat.{student_id}", **meta)
        self._record(student_id, trace_id)
        self._write_fragment(
            student_id=student_id, session_type="chat",
            trace_id=trace_id, payload={"message": message},
        )
        return trace_id

    def trace_interview(
        self,
        student_id: str,
        questionnaire_id: str,
        question_id: str,
        response: str,
    ) -> str:
        meta = self._base_metadata(
            student_id,
            "interview",
            questionnaire_id=questionnaire_id,
            question_id=question_id,
        )
        trace_id = self._provider.start_trace(
            f"classroom.interview.{student_id}.{question_id}", **meta
        )
        self._record(student_id, trace_id)
        self._write_fragment(
            student_id=student_id, session_type="interview",
            trace_id=trace_id,
            payload={
                "questionnaire_id": questionnaire_id,
                "question_id": question_id,
                "response": response,
            },
        )
        return trace_id

    def trace_quiz(
        self,
        student_id: str,
        quiz_id: str,
        question_id: str,
        response: str,
    ) -> str:
        meta = self._base_metadata(
            student_id,
            "quiz",
            quiz_id=quiz_id,
            question_id=question_id,
        )
        trace_id = self._provider.start_trace(f"classroom.quiz.{student_id}.{question_id}", **meta)
        self._record(student_id, trace_id)
        self._write_fragment(
            student_id=student_id, session_type="quiz",
            trace_id=trace_id,
            payload={
                "quiz_id": quiz_id,
                "question_id": question_id,
                "response": response,
            },
        )
        return trace_id

    def trace_research(self, student_id: str, topic: str, iteration: int = 1) -> str:
        meta = self._base_metadata(student_id, "research", topic=topic, iteration=iteration)
        trace_id = self._provider.start_trace(
            f"classroom.research.{student_id}.iter{iteration}", **meta
        )
        self._record(student_id, trace_id)
        self._write_fragment(
            student_id=student_id, session_type="research",
            trace_id=trace_id,
            payload={"topic": topic, "iteration": iteration},
        )
        return trace_id

    # -- delegation to underlying provider ---------------------------------

    def log_generation(
        self, trace_id: str, *, model: str, prompt: Any, output: Any, **meta: Any
    ) -> None:
        self._provider.log_generation(trace_id, model=model, prompt=prompt, output=output, **meta)

    def log_retrieval(self, trace_id: str, *, query: str, results: list[Any], **meta: Any) -> None:
        self._provider.log_retrieval(trace_id, query=query, results=results, **meta)

    def score(self, trace_id: str, *, name: str, value: float, **meta: Any) -> None:
        self._provider.score(trace_id, name=name, value=value, **meta)

    def flush(self) -> None:
        self._provider.flush()

    # -- analytics helpers -------------------------------------------------

    def get_student_traces(self, student_id: str) -> list[str]:
        return list(self._student_traces.get(student_id, []))

    def get_classroom_traces(self) -> list[str]:
        all_ids = []
        for ids in self._student_traces.values():
            all_ids.extend(ids)
        return all_ids

    def get_fragment_id_for_trace(self, trace_id: str) -> str | None:
        return self._trace_to_fragment.get(trace_id)

    def get_student_fragments(
        self, student_id: str, user: str, agent: str
    ) -> list[MemoryFragment]:
        """Read the student's trace fragments through the composition stack.

        Honors the bipartite access check — caller must have visibility
        into the student's agent+resource combination.
        """
        if self._composition is None:
            return []
        trace_ids = self._student_traces.get(student_id, [])
        fragment_ids = [
            self._trace_to_fragment[tid]
            for tid in trace_ids
            if tid in self._trace_to_fragment
        ]
        return self._composition.read(
            fragment_ids=fragment_ids, user=user, agent=agent,
        )
