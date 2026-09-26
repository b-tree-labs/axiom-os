# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for CHALKE (AI Training Assistant) v0 (#59)."""

from __future__ import annotations

import pytest


@pytest.fixture
def composition(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    from axiom.extensions.builtins.classroom.composition_boot import (
        build_classroom_composition,
    )

    return build_classroom_composition(classroom_id="cr-chalke")


@pytest.fixture
def chalke(composition):
    from axiom.extensions.builtins.classroom.agents.chalke import Chalke

    def stub_llm(messages, **kw):
        return "Stub response from CHALKE."

    return Chalke(
        classroom_id="cr-chalke",
        composition=composition,
        llm_backend=stub_llm,
    )


class TestIdentity:
    def test_name(self, chalke):
        assert chalke.name() == "CHALKE"

    def test_agent_id_canonical(self, chalke):
        assert chalke.agent_id() == "chalke"


class TestInstructorView:
    def test_daily_brief_empty_classroom(self, chalke):
        view = chalke.for_instructor()
        brief = view.daily_brief(instructor_id="@instr:ut")
        assert brief["classroom_id"] == "cr-chalke"
        assert brief["stuck_students"] == []
        assert brief["open_help_tickets"] == 0
        assert brief["top_priority"] is None

    def test_daily_brief_surfaces_stuck_signals(self, chalke, composition):
        from axiom.extensions.builtins.classroom.classroom_signals import (
            record_signal,
        )

        record_signal(
            composition,
            signal={
                "signal_type": "student_stuck",
                "student_id": "s1",
                "topic": "criticality",
                "turn_count": 7,
                "severity": "medium",
                "emitted_at": "2026-04-17T10:00:00Z",
            },
            classroom_id="cr-chalke",
            instructor_id="@instr:ut",
        )

        brief = chalke.for_instructor().daily_brief(instructor_id="@instr:ut")
        assert len(brief["stuck_students"]) == 1
        assert brief["stuck_students"][0]["student_id"] == "s1"
        assert brief["top_priority"]["kind"] == "stuck_student"

    def test_daily_brief_surfaces_misconceptions(self, chalke, composition):
        from axiom.extensions.builtins.classroom.classroom_signals import (
            record_signal,
        )

        record_signal(
            composition,
            signal={
                "signal_type": "misconception_detected",
                "student_id": "s2",
                "misconception_id": "confuses-mass-energy",
                "severity": "high",
                "emitted_at": "2026-04-17T10:00:00Z",
            },
            classroom_id="cr-chalke",
            instructor_id="@instr:ut",
        )

        brief = chalke.for_instructor().daily_brief(instructor_id="@instr:ut")
        assert brief["top_priority"]["kind"] == "misconception"

    def test_daily_brief_counts_open_tickets(self, chalke, composition):
        from axiom.extensions.builtins.classroom.help_tickets import (
            create_help_ticket,
            record_ticket,
        )

        t = create_help_ticket(
            student_id="s3", classroom_id="cr-chalke",
            issue="stuck", context_turns=[],
        )
        record_ticket(composition, t, instructor_id="@instr:ut")

        brief = chalke.for_instructor().daily_brief(instructor_id="@instr:ut")
        assert brief["open_help_tickets"] == 1


class TestStudentView:
    def test_explain_produces_teaching_plan(self, chalke):
        view = chalke.for_student("s1")
        result = view.explain(topic="fission")
        assert result["student_id"] == "s1"
        assert result["topic"] == "fission"
        # teaching intent → vector strategy + Frameworks floor
        assert result["plan"]["strategy"] == "vector"
        # Response populated by stub
        assert result["response"]

    def test_metacognitive_review_uses_trace_strategy(self, chalke):
        view = chalke.for_student("s1")
        result = view.metacognitive_review()
        assert result["plan_strategy"] == "trace"
        assert result["window_days"] == 30

    def test_profile_returns_none_before_creation(self, chalke):
        view = chalke.for_student("s1")
        assert view.profile() is None

    def test_update_profile_persists(self, chalke):
        view = chalke.for_student("s1")
        frag = view.update_profile(
            {"pedagogy_preference": "socratic", "language": "en"}
        )
        assert frag.cognitive_type.value == "core"
        assert frag.content["pedagogy_preference"] == "socratic"

    def test_profile_ownership_student_master_chalke_effort(self, chalke):
        from axiom.memory.ownership import Right, can_exercise

        view = chalke.for_student("s1")
        frag = view.update_profile({"language": "cs"})
        assert frag.ownership.master == "s1"
        at = "2026-06-01T00:00:00Z"
        assert can_exercise(frag.ownership, "chalke", Right.EFFORT, at)
        # CHALKE does NOT have CONTROL (can't delete the profile)
        assert not can_exercise(frag.ownership, "chalke", Right.CONTROL, at)

    def test_update_profile_merges_existing(self, chalke):
        view = chalke.for_student("s1")
        view.update_profile({"pedagogy_preference": "socratic"})
        view.update_profile({"language": "cs"})
        loaded = view.profile()
        # Both updates merged
        assert loaded.content["pedagogy_preference"] == "socratic"
        assert loaded.content["language"] == "cs"


class TestCompositionIntegration:
    def test_profile_writes_audit_entry(self, chalke, composition):
        chalke.for_student("s1").update_profile({"language": "en"})
        entries = list(composition.audit_log.read_all())
        writes = [e for e in entries if e["entry_type"] == "write"]
        assert len(writes) == 1

    def test_profile_fragment_signed(self, chalke, composition):
        from axiom.memory.attest import verify_fragment_signature

        frag = chalke.for_student("s1").update_profile({"x": 1})
        assert frag.signature is not None
        assert verify_fragment_signature(
            frag, composition.signing_keypair.public_bytes
        )
