# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for help tickets + SCAN signals + grade ledger composition (#73).

Three migrations, one test file.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def composition(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    from axiom.extensions.builtins.classroom.composition_boot import (
        build_classroom_composition,
    )

    return build_classroom_composition(classroom_id="cr-phase2c")


# ---------------------------------------------------------------------------
# Help tickets → procedural
# ---------------------------------------------------------------------------


class TestHelpTicketComposition:
    def test_ticket_records_as_procedural(self, composition):
        from axiom.extensions.builtins.classroom.help_tickets import (
            create_help_ticket,
            record_ticket,
        )

        ticket = create_help_ticket(
            student_id="s1", classroom_id="cr-phase2c",
            issue="stuck on decay chains",
            context_turns=[{"role": "user", "content": "help"}],
        )
        frag = record_ticket(composition, ticket, instructor_id="@instr:ut")
        assert frag.cognitive_type.value == "procedural"
        assert "steps" in frag.content
        assert "created" in frag.content["steps"]

    def test_acknowledged_adds_step(self, composition):
        from axiom.extensions.builtins.classroom.help_tickets import (
            acknowledge_ticket,
            create_help_ticket,
            record_ticket,
        )

        ticket = create_help_ticket(
            student_id="s1", classroom_id="cr-phase2c",
            issue="x", context_turns=[],
        )
        ticket = acknowledge_ticket(ticket, instructor_id="@instr:ut")
        frag = record_ticket(composition, ticket, instructor_id="@instr:ut")
        assert "acknowledged" in frag.content["steps"]

    def test_ownership_student_master_instructor_effort_control(self, composition):
        from axiom.extensions.builtins.classroom.help_tickets import (
            create_help_ticket,
            record_ticket,
        )
        from axiom.memory.ownership import Right, can_exercise

        ticket = create_help_ticket(
            student_id="s1", classroom_id="cr-phase2c",
            issue="x", context_turns=[],
        )
        frag = record_ticket(composition, ticket, instructor_id="@instr:ut")
        assert frag.ownership.master == "s1"
        at = "2026-06-01T00:00:00Z"
        assert can_exercise(frag.ownership, "@instr:ut", Right.CONTROL, at)
        assert can_exercise(frag.ownership, "@instr:ut", Right.EFFORT, at)
        assert not can_exercise(frag.ownership, "@instr:ut", Right.GOALS, at)
        assert not can_exercise(frag.ownership, "@instr:ut", Right.RESOURCES, at)


# ---------------------------------------------------------------------------
# SCAN signals → episodic
# ---------------------------------------------------------------------------


class TestSignalComposition:
    def test_stuck_signal_records_as_episodic(self, composition):
        from axiom.extensions.builtins.classroom.classroom_signals import (
            record_signal,
        )

        signal = {
            "signal_type": "student_stuck",
            "student_id": "s1",
            "topic": "critical mass",
            "turn_count": 7,
            "severity": "medium",
            "emitted_at": "2026-04-17T10:00:00Z",
        }
        frag = record_signal(
            composition, signal,
            classroom_id="cr-phase2c",
            instructor_id="@instr:ut",
        )
        assert frag.cognitive_type.value == "episodic"
        assert frag.content["signal_type"] == "student_stuck"
        assert frag.content["student_id"] == "s1"
        # Instructor is master; SCAN has EFFORT delegation
        from axiom.memory.ownership import Right, can_exercise

        at = "2026-06-01T00:00:00Z"
        assert frag.ownership.master == "@instr:ut"
        assert can_exercise(frag.ownership, "scan", Right.EFFORT, at)
        assert not can_exercise(frag.ownership, "scan", Right.CONTROL, at)

    def test_signal_added_to_signal_feed_resource(self, composition):
        from axiom.extensions.builtins.classroom.classroom_signals import (
            record_signal,
        )

        signal = {
            "signal_type": "low_engagement",
            "student_id": "s2",
            "severity": "medium",
            "emitted_at": "2026-04-17T10:00:00Z",
        }
        frag = record_signal(
            composition, signal,
            classroom_id="cr-phase2c",
            instructor_id="@instr:ut",
        )
        assert "signal-feed" in frag.provenance.resources


# ---------------------------------------------------------------------------
# Grade ledger → semantic (aggregate)
# ---------------------------------------------------------------------------


class TestGradeLedgerComposition:
    def test_ledger_entry_records_as_semantic(self, composition):
        from axiom.extensions.builtins.classroom.grade_push import (
            record_ledger_entry,
        )

        frag = record_ledger_entry(
            composition=composition,
            classroom_id="cr-phase2c",
            assessment_id="pre",
            student_id="s1",
            score=0.85,
            questions=10,
            instructor_id="@instr:ut",
        )
        assert frag.cognitive_type.value == "semantic"
        assert frag.content["score"] == 0.85
        assert frag.content["questions"] == 10
        assert frag.content["fact_kind"] == "grade_aggregate"

    def test_ledger_fragment_signed_and_audited(self, composition):
        from axiom.extensions.builtins.classroom.grade_push import (
            record_ledger_entry,
        )
        from axiom.memory.attest import verify_fragment_signature

        frag = record_ledger_entry(
            composition=composition,
            classroom_id="cr-phase2c",
            assessment_id="pre",
            student_id="s1",
            score=1.0,
            questions=5,
        )
        assert frag.signature is not None
        assert verify_fragment_signature(frag, composition.signing_keypair.public_bytes)

        entries = list(composition.audit_log.read_all())
        writes = [e for e in entries if e["entry_type"] == "write"]
        assert len(writes) == 1
