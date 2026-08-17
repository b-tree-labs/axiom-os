# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ClassroomTracer integration with CompositionService (#71).

The legacy trace_provider path still works; when composition is
wired, every trace_* call ALSO writes a MemoryFragment(episodic)
through the full stack (policy → sign → persist → audit).
"""

from __future__ import annotations

import pytest


@pytest.fixture
def composition(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    from axiom.extensions.builtins.classroom.composition_boot import (
        build_classroom_composition,
    )

    return build_classroom_composition(classroom_id="cr-test")


@pytest.fixture
def tracer_with_composition(composition):
    from axiom.extensions.builtins.classroom.tracing import ClassroomTracer
    from axiom.infra.tracing import InMemoryTraceProvider

    return ClassroomTracer(
        classroom_id="cr-test",
        course_id="course-x",
        trace_provider=InMemoryTraceProvider(),
        composition=composition,
    )


class TestChatTraceProducesFragment:
    def test_chat_call_writes_episodic_fragment(self, tracer_with_composition):
        trace_id = tracer_with_composition.trace_chat(
            student_id="s1", message="What is fission?"
        )
        # The legacy API still returns a trace_id
        assert trace_id

        # The composition-stack side: fragment exists
        fragment_id = tracer_with_composition.get_fragment_id_for_trace(trace_id)
        assert fragment_id is not None

    def test_fragment_carries_expected_shape(self, tracer_with_composition, composition):
        from axiom.memory.fragment import fragment_from_dict

        trace_id = tracer_with_composition.trace_chat(
            student_id="s1", message="What is critical mass?"
        )
        fragment_id = tracer_with_composition.get_fragment_id_for_trace(trace_id)

        # Find the fragment via the registry
        artifact = [
            a for a in composition.artifact_registry.list(kind="fragment")
            if a.name == fragment_id
        ][0]
        frag = fragment_from_dict(artifact.data)

        assert frag.cognitive_type.value == "episodic"
        assert frag.provenance.principal_id == "s1"
        assert "axi" in frag.provenance.agents
        assert "classroom:cr-test" in frag.provenance.resources
        assert frag.content["session_type"] == "chat"
        assert frag.content["classroom_id"] == "cr-test"
        assert frag.content["course_id"] == "course-x"
        assert frag.content["message"] == "What is critical mass?"

    def test_fragment_is_signed(self, tracer_with_composition, composition):
        from axiom.memory.attest import verify_fragment_signature
        from axiom.memory.fragment import fragment_from_dict

        trace_id = tracer_with_composition.trace_chat(
            student_id="s1", message="hi"
        )
        fragment_id = tracer_with_composition.get_fragment_id_for_trace(trace_id)

        artifact = [
            a for a in composition.artifact_registry.list(kind="fragment")
            if a.name == fragment_id
        ][0]
        frag = fragment_from_dict(artifact.data)
        assert frag.signature is not None
        assert verify_fragment_signature(frag, composition.signing_keypair.public_bytes)

    def test_audit_log_has_write_entry(self, tracer_with_composition, composition):
        tracer_with_composition.trace_chat(student_id="s1", message="hi")
        entries = list(composition.audit_log.read_all())
        writes = [e for e in entries if e["entry_type"] == "write"]
        assert len(writes) == 1
        assert writes[0]["principal_id"] == "s1"


class TestOtherSessionTypes:
    def test_interview_trace(self, tracer_with_composition, composition):
        from axiom.memory.fragment import fragment_from_dict

        trace_id = tracer_with_composition.trace_interview(
            student_id="s1",
            questionnaire_id="begin",
            question_id="Q1",
            response="physics background",
        )
        tracer_with_composition.get_fragment_id_for_trace(trace_id)
        artifact = composition.artifact_registry.list(kind="fragment")[0]
        frag = fragment_from_dict(artifact.data)
        assert frag.content["session_type"] == "interview"
        assert frag.content["questionnaire_id"] == "begin"
        assert frag.content["question_id"] == "Q1"

    def test_quiz_trace(self, tracer_with_composition, composition):
        from axiom.memory.fragment import fragment_from_dict

        trace_id = tracer_with_composition.trace_quiz(
            student_id="s1", quiz_id="pre", question_id="Q1", response="B",
        )
        tracer_with_composition.get_fragment_id_for_trace(trace_id)
        artifact = composition.artifact_registry.list(kind="fragment")[0]
        frag = fragment_from_dict(artifact.data)
        assert frag.content["session_type"] == "quiz"
        assert frag.content["quiz_id"] == "pre"

    def test_research_trace(self, tracer_with_composition, composition):
        from axiom.memory.fragment import fragment_from_dict

        trace_id = tracer_with_composition.trace_research(
            student_id="s1", topic="critical mass", iteration=2,
        )
        tracer_with_composition.get_fragment_id_for_trace(trace_id)
        artifact = composition.artifact_registry.list(kind="fragment")[0]
        frag = fragment_from_dict(artifact.data)
        assert frag.content["session_type"] == "research"
        assert frag.content["topic"] == "critical mass"
        assert frag.content["iteration"] == 2


class TestGetStudentFragments:
    def test_read_student_fragments_through_access(
        self, tracer_with_composition, composition
    ):
        from axiom.memory.access import (
            add_agent_resource_edge,
            add_user_agent_edge,
        )

        tracer_with_composition.trace_chat(student_id="s1", message="hi")
        tracer_with_composition.trace_chat(student_id="s1", message="follow-up")

        # Without access edges, reads are denied
        no_access = tracer_with_composition.get_student_fragments(
            student_id="s1", user="@instructor", agent="axi"
        )
        assert no_access == []

        # Grant access
        composition.access_graphs = add_user_agent_edge(
            composition.access_graphs, "@instructor", "axi"
        )
        composition.access_graphs = add_agent_resource_edge(
            composition.access_graphs, "axi", "classroom:cr-test"
        )
        fragments = tracer_with_composition.get_student_fragments(
            student_id="s1", user="@instructor", agent="axi"
        )
        assert len(fragments) == 2


class TestLegacyPath:
    """Without composition wired, tracer behaves as before."""

    def test_no_composition_no_fragments(self):
        from axiom.extensions.builtins.classroom.tracing import ClassroomTracer
        from axiom.infra.tracing import InMemoryTraceProvider

        tracer = ClassroomTracer(
            classroom_id="cr", course_id="c",
            trace_provider=InMemoryTraceProvider(),
            composition=None,
        )
        trace_id = tracer.trace_chat(student_id="s1", message="hi")
        assert trace_id
        assert tracer.get_fragment_id_for_trace(trace_id) is None
