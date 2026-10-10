# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for help-ticket backend (WF-6 / §5.6).

Per spec-classroom.md §3.1 WF-6: student /help triggers help ticket
with context, routes to instructor queue, supports remediation plan,
tracks status lifecycle (open → in_progress → resolved).

Standalone: in-memory store + local persistence.
"""

from __future__ import annotations


def _ctx_turns():
    return [
        {"role": "user", "content": "I don't understand how critical mass works"},
        {"role": "assistant", "content": "Critical mass is..."},
        {"role": "user", "content": "but I tried that and got different results"},
    ]


class TestCreateTicket:
    def test_from_help_command(self):
        from axiom.extensions.builtins.classroom.help_tickets import (
            create_help_ticket,
        )

        ticket = create_help_ticket(
            student_id="s1",
            classroom_id="cr",
            issue="I'm stuck on critical mass calculations",
            context_turns=_ctx_turns(),
        )

        assert ticket.student_id == "s1"
        assert ticket.classroom_id == "cr"
        assert ticket.status == "open"
        assert ticket.ticket_id  # uuid generated
        assert len(ticket.context_turns) == 3
        assert ticket.issue == "I'm stuck on critical mass calculations"
        assert ticket.created_at  # ISO timestamp


class TestInstructorQueue:
    def test_queue_shows_open_tickets_only(self):
        from axiom.extensions.builtins.classroom.help_tickets import (
            HelpTicket,
            instructor_queue,
        )

        tickets = [
            HelpTicket(ticket_id="t1", student_id="s1", classroom_id="cr",
                       issue="x", status="open"),
            HelpTicket(ticket_id="t2", student_id="s2", classroom_id="cr",
                       issue="y", status="resolved"),
            HelpTicket(ticket_id="t3", student_id="s3", classroom_id="cr",
                       issue="z", status="in_progress"),
        ]
        queue = instructor_queue(tickets, classroom_id="cr")
        statuses = {t.status for t in queue}
        assert statuses == {"open", "in_progress"}
        assert len(queue) == 2

    def test_queue_filters_by_classroom(self):
        from axiom.extensions.builtins.classroom.help_tickets import (
            HelpTicket,
            instructor_queue,
        )

        tickets = [
            HelpTicket(ticket_id="t1", student_id="s1", classroom_id="cr-a",
                       issue="x", status="open"),
            HelpTicket(ticket_id="t2", student_id="s2", classroom_id="cr-b",
                       issue="y", status="open"),
        ]
        queue = instructor_queue(tickets, classroom_id="cr-a")
        assert len(queue) == 1
        assert queue[0].ticket_id == "t1"


class TestTicketTransitions:
    def test_acknowledge_moves_to_in_progress(self):
        from axiom.extensions.builtins.classroom.help_tickets import (
            acknowledge_ticket,
            create_help_ticket,
        )

        ticket = create_help_ticket(
            student_id="s1", classroom_id="cr", issue="x", context_turns=[]
        )
        acked = acknowledge_ticket(ticket, instructor_id="ben@ut.edu")
        assert acked.status == "in_progress"
        assert acked.acknowledged_by == "ben@ut.edu"
        assert acked.acknowledged_at

    def test_resolve_marks_resolved(self):
        from axiom.extensions.builtins.classroom.help_tickets import (
            create_help_ticket,
            resolve_ticket,
        )

        ticket = create_help_ticket(
            student_id="s1", classroom_id="cr", issue="x", context_turns=[]
        )
        resolved = resolve_ticket(
            ticket, resolver="ben@ut.edu", resolution="Worked through it 1:1."
        )
        assert resolved.status == "resolved"
        assert resolved.resolution == "Worked through it 1:1."
        assert resolved.resolved_by == "ben@ut.edu"


class TestRemediationPlan:
    def test_attach_plan_to_ticket(self):
        from axiom.extensions.builtins.classroom.help_tickets import (
            attach_remediation_plan,
            create_help_ticket,
        )

        ticket = create_help_ticket(
            student_id="s1", classroom_id="cr",
            issue="stuck on critical mass", context_turns=[],
        )
        plan = {
            "readings": ["ch3.pdf", "glasstone-ch7"],
            "practice_problems": ["PQ-3", "PQ-4"],
            "follow_up_by": "2026-04-25",
        }
        with_plan = attach_remediation_plan(ticket, plan)
        assert with_plan.remediation_plan == plan

    def test_plan_does_not_auto_resolve(self):
        from axiom.extensions.builtins.classroom.help_tickets import (
            attach_remediation_plan,
            create_help_ticket,
        )

        ticket = create_help_ticket(
            student_id="s1", classroom_id="cr", issue="x", context_turns=[]
        )
        with_plan = attach_remediation_plan(ticket, {"readings": ["x"]})
        assert with_plan.status == "open"  # still open; student must complete plan


class TestPersistence:
    def test_save_and_load_tickets(self, tmp_path):
        from axiom.extensions.builtins.classroom.help_tickets import (
            create_help_ticket,
            load_tickets,
            save_tickets,
        )

        ticket = create_help_ticket(
            student_id="s1", classroom_id="cr",
            issue="halp", context_turns=_ctx_turns(),
        )

        path = tmp_path / "tickets.json"
        save_tickets([ticket], path)
        assert path.exists()

        loaded = load_tickets(path)
        assert len(loaded) == 1
        assert loaded[0].ticket_id == ticket.ticket_id
        assert loaded[0].issue == "halp"
        assert len(loaded[0].context_turns) == 3


class TestChatIntegration:
    """Pipeline hook: when /help is seen, a ticket is created."""

    def test_parse_help_from_chat_turn(self):
        from axiom.extensions.builtins.classroom.help_tickets import (
            parse_help_command,
        )

        # /help <issue>
        parsed = parse_help_command("/help I'm stuck on critical mass")
        assert parsed is not None
        assert parsed == "I'm stuck on critical mass"

    def test_non_help_returns_none(self):
        from axiom.extensions.builtins.classroom.help_tickets import (
            parse_help_command,
        )

        assert parse_help_command("regular message") is None
        assert parse_help_command("/research what is fission?") is None

    def test_bare_help_returns_empty_string(self):
        from axiom.extensions.builtins.classroom.help_tickets import (
            parse_help_command,
        )

        # /help alone should still trigger — instructor can ask student
        assert parse_help_command("/help") == ""


class TestFederationSignedTicket:
    def test_serialize_ticket_claim(self):
        from axiom.extensions.builtins.classroom.help_tickets import (
            create_help_ticket,
            serialize_ticket_claim,
        )

        ticket = create_help_ticket(
            student_id="s1", classroom_id="cr",
            issue="stuck", context_turns=[],
        )
        claim = serialize_ticket_claim(ticket, signer_node="prague.axiom.eu")
        assert claim["student_id"] == "s1"
        assert claim["signer_node"] == "prague.axiom.eu"
        assert claim["issue"] == "stuck"
        assert "signature" in claim
