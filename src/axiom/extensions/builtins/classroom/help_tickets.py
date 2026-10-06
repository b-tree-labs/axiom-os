# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Help-ticket backend (WF-6 / spec-classroom.md §3.1).

Students invoke /help in chat → ticket created with recent turn
context → routed to instructor queue → optional remediation plan
→ resolved. Pure data model + functions; persistence is caller
responsibility (save_tickets / load_tickets for JSON round-trip).

Standalone: local JSON storage. Federation stretch: serialize_ticket_
claim() produces a signed cross-node payload so help requests from
students on member nodes reach the hub-node instructor.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from axiom.infra.identifiers import generate_id

if TYPE_CHECKING:
    from axiom.memory.composition import CompositionService
    from axiom.memory.fragment import MemoryFragment


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class HelpTicket:
    """A student's help request with lifecycle state."""

    ticket_id: str = ""
    student_id: str = ""
    classroom_id: str = ""
    issue: str = ""
    status: str = "open"  # open, in_progress, resolved

    context_turns: list[dict] = field(default_factory=list)
    remediation_plan: dict | None = None

    created_at: str | None = None
    acknowledged_by: str | None = None
    acknowledged_at: str | None = None
    resolved_by: str | None = None
    resolved_at: str | None = None
    resolution: str | None = None


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def create_help_ticket(
    student_id: str,
    classroom_id: str,
    issue: str,
    context_turns: list[dict],
) -> HelpTicket:
    """Create a new open ticket with auto-generated id."""
    return HelpTicket(
        ticket_id=generate_id(),
        student_id=student_id,
        classroom_id=classroom_id,
        issue=issue,
        status="open",
        context_turns=list(context_turns),
        created_at=_now_iso(),
    )


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def acknowledge_ticket(ticket: HelpTicket, instructor_id: str) -> HelpTicket:
    """Instructor acknowledges → in_progress."""
    t = deepcopy(ticket)
    t.status = "in_progress"
    t.acknowledged_by = instructor_id
    t.acknowledged_at = _now_iso()
    return t


def attach_remediation_plan(
    ticket: HelpTicket, plan: dict
) -> HelpTicket:
    """Attach a remediation plan. Does NOT auto-resolve."""
    t = deepcopy(ticket)
    t.remediation_plan = dict(plan)
    return t


def resolve_ticket(
    ticket: HelpTicket, resolver: str, resolution: str
) -> HelpTicket:
    """Mark resolved with a note."""
    t = deepcopy(ticket)
    t.status = "resolved"
    t.resolved_by = resolver
    t.resolution = resolution
    t.resolved_at = _now_iso()
    return t


# ---------------------------------------------------------------------------
# Instructor queue
# ---------------------------------------------------------------------------


def instructor_queue(
    tickets: list[HelpTicket], classroom_id: str
) -> list[HelpTicket]:
    """Return open + in_progress tickets for one classroom."""
    return [
        t for t in tickets
        if t.classroom_id == classroom_id and t.status in ("open", "in_progress")
    ]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_tickets(tickets: list[HelpTicket], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(t) for t in tickets], indent=2))


def load_tickets(path: Path) -> list[HelpTicket]:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return [HelpTicket(**t) for t in data]


# ---------------------------------------------------------------------------
# Chat pipeline integration
# ---------------------------------------------------------------------------


def parse_help_command(text: str) -> str | None:
    """Parse a chat line for the /help command.

    Returns the issue string (possibly empty) if the line starts with
    /help, else None.
    """
    stripped = text.strip()
    if stripped == "/help":
        return ""
    if stripped.startswith("/help "):
        return stripped[len("/help "):].strip()
    return None


# ---------------------------------------------------------------------------
# Federation claim (ADR-023)
# ---------------------------------------------------------------------------


def serialize_ticket_claim(ticket: HelpTicket, signer_node: str) -> dict:
    """Produce a signed-claim dict for cross-node help routing.

    When a student on a member node invokes /help, the claim flows to
    the instructor's hub node for queuing. Signature verified by
    federation layer before accepting.
    """
    payload = asdict(ticket)
    payload["signer_node"] = signer_node
    payload["signature"] = None  # federation layer fills in
    return payload


# ---------------------------------------------------------------------------
# Composition integration (#73)
# ---------------------------------------------------------------------------


def record_ticket(
    composition: CompositionService,
    ticket: HelpTicket,
    instructor_id: str | None = None,
) -> MemoryFragment:
    """Materialize a HelpTicket as MemoryFragment(procedural).

    A help ticket is a workflow: open → in_progress → resolved. Procedural
    is the right cognitive type — steps matter, effectiveness is scorable.

    Ownership: student = master; instructor gets CONTROL+EFFORT delegation
    (can acknowledge + resolve + put in the work). Per ADR-026.

    On status transitions the caller re-records the ticket — supersedure
    handles the version chain (old fragment's valid_time_end closes).
    """
    from axiom.memory.ownership import (
        Right,
        new_ownership,
    )
    from axiom.memory.ownership import (
        delegate as _delegate,
    )

    own = new_ownership(master=ticket.student_id)
    if instructor_id:
        own = _delegate(
            own,
            delegate_principal=instructor_id,
            rights={Right.CONTROL, Right.EFFORT},
            expires_at="2099-12-31T23:59:59Z",
        )

    return composition.write(
        content={
            "ticket_id": ticket.ticket_id,
            "student_id": ticket.student_id,
            "classroom_id": ticket.classroom_id,
            "issue": ticket.issue,
            "status": ticket.status,
            "context_turns": ticket.context_turns,
            "remediation_plan": ticket.remediation_plan,
            "created_at": ticket.created_at,
            "acknowledged_by": ticket.acknowledged_by,
            "acknowledged_at": ticket.acknowledged_at,
            "resolved_by": ticket.resolved_by,
            "resolved_at": ticket.resolved_at,
            "resolution": ticket.resolution,
            "steps": _lifecycle_steps(ticket),
        },
        cognitive_type="procedural",
        principal_id=ticket.student_id,
        agents={"chalke"},
        resources={
            f"classroom:{ticket.classroom_id}",
            "help-queue",
        },
        ownership=own,
    )


def _lifecycle_steps(ticket: HelpTicket) -> list[str]:
    """The procedural 'steps' field reflects ticket lifecycle position."""
    steps = ["created"]
    if ticket.acknowledged_at:
        steps.append("acknowledged")
    if ticket.remediation_plan:
        steps.append("remediation_planned")
    if ticket.resolved_at:
        steps.append("resolved")
    return steps
