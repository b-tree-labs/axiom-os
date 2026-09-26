# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for classroom-specific tracing layer.

Wraps the existing LangfuseTraceProvider with classroom attribution:
student_id, classroom_id, course_id, session_type (chat, interview,
quiz, research). Enables instructor analytics queries.
"""

from __future__ import annotations


class TestClassroomTracerInit:
    def test_creates_with_attribution(self):
        from axiom.extensions.builtins.classroom.tracing import ClassroomTracer

        tracer = ClassroomTracer(
            classroom_id="cls-prague-2026",
            course_id="ne-stem-2026",
            trace_provider=None,  # uses NullTraceProvider
        )
        assert tracer.classroom_id == "cls-prague-2026"
        assert tracer.course_id == "ne-stem-2026"


class TestStudentTraceAttribution:
    def test_trace_tagged_with_student_and_classroom(self):
        from axiom.extensions.builtins.classroom.tracing import ClassroomTracer
        from axiom.infra.tracing import InMemoryTraceProvider

        mem = InMemoryTraceProvider()
        tracer = ClassroomTracer(
            classroom_id="cls-test",
            course_id="course-1",
            trace_provider=mem,
        )

        trace_id = tracer.trace_chat(student_id="s1", message="What is fission?")
        assert trace_id is not None

        # InMemoryTraceProvider stores traces for inspection
        trace = mem.get_trace(trace_id)
        assert trace is not None
        assert trace["metadata"]["student_id"] == "s1"
        assert trace["metadata"]["classroom_id"] == "cls-test"
        assert trace["metadata"]["course_id"] == "course-1"
        assert trace["metadata"]["session_type"] == "chat"

    def test_trace_interview_session_type(self):
        from axiom.extensions.builtins.classroom.tracing import ClassroomTracer
        from axiom.infra.tracing import InMemoryTraceProvider

        mem = InMemoryTraceProvider()
        tracer = ClassroomTracer(
            classroom_id="cls-test",
            course_id="c1",
            trace_provider=mem,
        )

        trace_id = tracer.trace_interview(
            student_id="s2",
            questionnaire_id="begin-of-course",
            question_id="Q1",
            response="I use AI daily",
        )

        trace = mem.get_trace(trace_id)
        assert trace["metadata"]["session_type"] == "interview"
        assert trace["metadata"]["questionnaire_id"] == "begin-of-course"
        assert trace["metadata"]["question_id"] == "Q1"

    def test_trace_quiz_session_type(self):
        from axiom.extensions.builtins.classroom.tracing import ClassroomTracer
        from axiom.infra.tracing import InMemoryTraceProvider

        mem = InMemoryTraceProvider()
        tracer = ClassroomTracer(
            classroom_id="cls-test",
            course_id="c1",
            trace_provider=mem,
        )

        trace_id = tracer.trace_quiz(
            student_id="s1",
            quiz_id="midterm",
            question_id="Q5",
            response="Chain reaction sustains fission",
        )

        trace = mem.get_trace(trace_id)
        assert trace["metadata"]["session_type"] == "quiz"
        assert trace["metadata"]["quiz_id"] == "midterm"


class TestLogGeneration:
    def test_log_llm_generation_with_attribution(self):
        from axiom.extensions.builtins.classroom.tracing import ClassroomTracer
        from axiom.infra.tracing import InMemoryTraceProvider

        mem = InMemoryTraceProvider()
        tracer = ClassroomTracer(
            classroom_id="cls-test",
            course_id="c1",
            trace_provider=mem,
        )

        trace_id = tracer.trace_chat(student_id="s1", message="Hello")
        tracer.log_generation(
            trace_id,
            model="bonsai-local",
            prompt=[{"role": "user", "content": "Hello"}],
            output="Hi there! Welcome to the course.",
        )

        gen = mem.get_generations(trace_id)
        assert len(gen) == 1
        assert gen[0]["model"] == "bonsai-local"


class TestLogRetrieval:
    def test_log_rag_retrieval_with_attribution(self):
        from axiom.extensions.builtins.classroom.tracing import ClassroomTracer
        from axiom.infra.tracing import InMemoryTraceProvider

        mem = InMemoryTraceProvider()
        tracer = ClassroomTracer(
            classroom_id="cls-test",
            course_id="c1",
            trace_provider=mem,
        )

        trace_id = tracer.trace_chat(student_id="s1", message="Explain fission")
        tracer.log_retrieval(
            trace_id,
            query="Explain fission",
            results=[
                {"text": "Fission splits atoms.", "source": "ch3"},
            ],
        )

        retrievals = mem.get_retrievals(trace_id)
        assert len(retrievals) == 1
        assert retrievals[0]["query"] == "Explain fission"


class TestCohortAnalytics:
    def test_get_traces_for_student(self):
        from axiom.extensions.builtins.classroom.tracing import ClassroomTracer
        from axiom.infra.tracing import InMemoryTraceProvider

        mem = InMemoryTraceProvider()
        tracer = ClassroomTracer(
            classroom_id="cls-test",
            course_id="c1",
            trace_provider=mem,
        )

        tracer.trace_chat(student_id="s1", message="Q1")
        tracer.trace_chat(student_id="s2", message="Q2")
        tracer.trace_chat(student_id="s1", message="Q3")

        s1_traces = tracer.get_student_traces("s1")
        assert len(s1_traces) == 2

    def test_get_all_classroom_traces(self):
        from axiom.extensions.builtins.classroom.tracing import ClassroomTracer
        from axiom.infra.tracing import InMemoryTraceProvider

        mem = InMemoryTraceProvider()
        tracer = ClassroomTracer(
            classroom_id="cls-test",
            course_id="c1",
            trace_provider=mem,
        )

        tracer.trace_chat(student_id="s1", message="Q1")
        tracer.trace_chat(student_id="s2", message="Q2")

        all_traces = tracer.get_classroom_traces()
        assert len(all_traces) == 2
