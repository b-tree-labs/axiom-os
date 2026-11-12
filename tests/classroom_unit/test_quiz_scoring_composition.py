# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ScoredResponse → MemoryFragment(semantic) migration (#72)."""

from __future__ import annotations

import pytest


@pytest.fixture
def composition(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    from axiom.extensions.builtins.classroom.composition_boot import (
        build_classroom_composition,
    )

    return build_classroom_composition(classroom_id="cr-quiz")


def _scored(student_id: str = "s1", final: float = 1.0):
    from axiom.extensions.builtins.classroom.quiz_scoring import ScoredResponse

    return ScoredResponse(
        student_id=student_id, assessment_id="pre",
        question_id="Q1", question_type="mcq",
        auto_score=final, final_score=final, needs_review=False,
    )


class TestRecordScoredResponse:
    def test_produces_semantic_fragment(self, composition):
        from axiom.extensions.builtins.classroom.quiz_scoring import (
            record_scored_response,
        )

        scored = _scored()
        frag = record_scored_response(
            composition=composition, scored=scored, classroom_id="cr-quiz",
        )
        assert frag.cognitive_type.value == "semantic"
        assert frag.provenance.principal_id == "s1"

    def test_fragment_carries_score_content(self, composition):
        from axiom.extensions.builtins.classroom.quiz_scoring import (
            record_scored_response,
        )

        frag = record_scored_response(
            composition=composition, scored=_scored(final=0.75),
            classroom_id="cr-quiz",
        )
        assert frag.content["final_score"] == 0.75
        assert frag.content["assessment_id"] == "pre"
        assert frag.content["question_id"] == "Q1"

    def test_ownership_student_master_instructor_delegate(self, composition):
        from axiom.extensions.builtins.classroom.quiz_scoring import (
            record_scored_response,
        )
        from axiom.memory.ownership import Right, can_exercise

        frag = record_scored_response(
            composition=composition,
            scored=_scored("s1"),
            classroom_id="cr-quiz",
            instructor_id="@instructor:ut",
        )
        # Student is master
        assert frag.ownership.master == "s1"
        # Instructor has CONTROL + GOALS but not RESOURCES or EFFORT
        at = "2026-06-01T00:00:00Z"
        assert can_exercise(frag.ownership, "@instructor:ut", Right.CONTROL, at)
        assert can_exercise(frag.ownership, "@instructor:ut", Right.GOALS, at)
        assert not can_exercise(
            frag.ownership, "@instructor:ut", Right.RESOURCES, at
        )
        assert not can_exercise(
            frag.ownership, "@instructor:ut", Right.EFFORT, at
        )

    def test_fragment_signed_and_audited(self, composition):
        from axiom.extensions.builtins.classroom.quiz_scoring import (
            record_scored_response,
        )
        from axiom.memory.attest import verify_fragment_signature

        frag = record_scored_response(
            composition=composition, scored=_scored(),
            classroom_id="cr-quiz",
        )
        # Signed
        assert frag.signature is not None
        assert verify_fragment_signature(frag, composition.signing_keypair.public_bytes)
        # Audit entry
        entries = list(composition.audit_log.read_all())
        writes = [e for e in entries if e["entry_type"] == "write"]
        assert len(writes) == 1
        assert writes[0]["fragment_id"] == frag.id

    def test_resources_include_assessment_and_classroom(self, composition):
        from axiom.extensions.builtins.classroom.quiz_scoring import (
            record_scored_response,
        )

        frag = record_scored_response(
            composition=composition, scored=_scored(),
            classroom_id="cr-quiz",
        )
        assert "assessment:pre" in frag.provenance.resources
        assert "classroom:cr-quiz" in frag.provenance.resources


class TestReadThroughAccess:
    def test_instructor_access_to_student_grades(self, composition):
        from axiom.extensions.builtins.classroom.quiz_scoring import (
            record_scored_response,
        )
        from axiom.memory.access import (
            add_agent_resource_edge,
            add_user_agent_edge,
        )

        frag = record_scored_response(
            composition=composition, scored=_scored("s1"),
            classroom_id="cr-quiz", instructor_id="@instr:ut",
        )
        # No access edges → instructor can't read
        results = composition.read(
            fragment_ids=[frag.id],
            user="@instr:ut", agent="chalke",
        )
        assert results == []

        # Grant access through chalke → assessment + classroom
        composition.access_graphs = add_user_agent_edge(
            composition.access_graphs, "@instr:ut", "chalke"
        )
        composition.access_graphs = add_agent_resource_edge(
            composition.access_graphs, "chalke", "assessment:pre"
        )
        composition.access_graphs = add_agent_resource_edge(
            composition.access_graphs, "chalke", "classroom:cr-quiz"
        )
        results = composition.read(
            fragment_ids=[frag.id],
            user="@instr:ut", agent="chalke",
        )
        assert len(results) == 1
        assert results[0].content["final_score"] == 1.0
