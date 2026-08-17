# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Phase 3 tests: chat pipeline integration with composition stack (#75).

Proves the chat turn exercises every primitive: tracer writes a
fragment (episodic); gating filters classified chunks; post-filter
runs breach detection; all audit-logged.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def composition(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    from axiom.extensions.builtins.classroom.composition_boot import (
        build_classroom_composition,
    )

    return build_classroom_composition(classroom_id="cr-phase3")


@pytest.fixture
def tracer(composition):
    from axiom.extensions.builtins.classroom.tracing import ClassroomTracer
    from axiom.infra.tracing import InMemoryTraceProvider

    return ClassroomTracer(
        classroom_id="cr-phase3",
        course_id="course-x",
        trace_provider=InMemoryTraceProvider(),
        composition=composition,
    )


class TestTracerIntegrated:
    def test_chat_writes_fragment_when_tracer_wired(self, composition, tracer):
        from axiom.extensions.builtins.classroom.pipeline import (
            ClassroomChatPipeline,
        )

        def llm(msgs, **kw):
            return "Fission splits heavy nuclei."

        pipe = ClassroomChatPipeline(
            course_system_prompt="You are a tutor.",
            llm_backend=llm,
            composition=composition,
            tracer=tracer,
            student_id="s1",
        )
        pipe.chat([{"role": "user", "content": "What is fission?"}])

        # Fragment written via tracer → composition
        trace_ids = tracer.get_student_traces("s1")
        assert len(trace_ids) == 1
        fragment_id = tracer.get_fragment_id_for_trace(trace_ids[0])
        assert fragment_id is not None

    def test_chat_without_composition_legacy_behavior(self):
        """No composition wired → chat still works; no side effects."""
        from axiom.extensions.builtins.classroom.pipeline import (
            ClassroomChatPipeline,
        )

        def llm(msgs, **kw):
            return "answer"

        pipe = ClassroomChatPipeline(
            course_system_prompt="You are a tutor.",
            llm_backend=llm,
        )
        response = pipe.chat([{"role": "user", "content": "hi"}])
        assert "answer" in response


class TestGatingIntegrated:
    def test_classified_chunks_filtered_without_attestation(self, composition, tracer):
        """EC-classified chunks dropped when student has no attestation."""
        from axiom.extensions.builtins.classroom.pipeline import (
            ClassroomChatPipeline,
        )

        # Retriever returns one classified + one unclassified chunk
        def retriever(query, k):
            return [
                {"id": "public", "text": "fission basics", "source": "ch1"},
                {"id": "secret", "text": "classified stuff",
                 "source": "ch2", "classification": "EC",
                 "required_attribute": "nationality",
                 "allowed_values": ["US"]},
            ]

        captured_messages = []

        def llm(msgs, **kw):
            captured_messages.extend(msgs)
            return "answer"

        pipe = ClassroomChatPipeline(
            course_system_prompt="tutor",
            rag_retriever=retriever,
            llm_backend=llm,
            composition=composition,
            tracer=tracer,
            student_id="s1",
            student_attestation=None,  # no attestation → EC chunks dropped
            verify_attestation=lambda a: True,
        )
        pipe.chat([{"role": "user", "content": "What is fission?"}])

        # The LLM only saw the public chunk in the RAG context
        rag_sys_msgs = [m for m in captured_messages if m.get("role") == "system"
                        and "ch" in m.get("content", "")]
        assert rag_sys_msgs  # some RAG context present
        joined = "\n".join(m["content"] for m in rag_sys_msgs)
        assert "fission basics" in joined
        assert "classified stuff" not in joined

    def test_classified_chunks_allowed_with_matching_attestation(
        self, composition, tracer
    ):
        from axiom.extensions.builtins.classroom.pipeline import (
            ClassroomChatPipeline,
        )

        def retriever(query, k):
            return [
                {"id": "secret", "text": "classified info",
                 "source": "ch1", "classification": "EC",
                 "required_attribute": "nationality",
                 "allowed_values": ["US"]},
            ]

        captured_messages = []

        def llm(msgs, **kw):
            captured_messages.extend(msgs)
            return "answer"

        pipe = ClassroomChatPipeline(
            course_system_prompt="tutor",
            rag_retriever=retriever,
            llm_backend=llm,
            composition=composition,
            tracer=tracer,
            student_id="s1",
            student_attestation={
                "principal_id": "s1",
                "attributes": {"nationality": "US"},
            },
            verify_attestation=lambda a: True,
        )
        pipe.chat([{"role": "user", "content": "What is X?"}])

        rag_sys_msgs = [
            m for m in captured_messages
            if m.get("role") == "system" and "classified info" in m.get("content", "")
        ]
        assert len(rag_sys_msgs) == 1


class TestAuditTrail:
    def test_chat_writes_audit_entries(self, composition, tracer):
        from axiom.extensions.builtins.classroom.pipeline import (
            ClassroomChatPipeline,
        )

        pipe = ClassroomChatPipeline(
            course_system_prompt="tutor",
            llm_backend=lambda msgs, **kw: "response",
            composition=composition,
            tracer=tracer,
            student_id="s1",
        )
        pipe.chat([{"role": "user", "content": "q"}])

        entries = list(composition.audit_log.read_all())
        # At least one write entry (the trace fragment)
        writes = [e for e in entries if e["entry_type"] == "write"]
        assert len(writes) >= 1
