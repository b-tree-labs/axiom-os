# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Receipts-side persistence: the day focus, the delta baseline, and
the DECISION RECORD.

Claim verdicts live with their sources (fleet today; session/seat/twin
later). What lives here is what the oversight loop itself owns: the
focus directive (K2/R12, with provenance), the previous verdict
snapshot so trust DELTAS are computed against recorded state (R14),
and — the differentiator — every decision a person made about a case,
as a typed decision receipt (ADR-126) whose outcome arrives later.

The decision record is the thing a status board does not have: not
"what is broken" but "what did we decide, who decided it, on what
evidence, and did it work".
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Focus(Base):
    """The current day-focus directive (R12). One row per site scope.

    ``set_by`` is the provenance the surface must always render (human
    or system; ``@principal`` handle). A PROPOSED focus (the MCP
    propose floor) lands with ``state='proposed'`` and never displaces
    an active one until confirmed through a gated surface.
    """

    __tablename__ = "focus"

    site: Mapped[str] = mapped_column(String(200), primary_key=True)
    text: Mapped[str] = mapped_column(String(2000))
    set_by: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(20), default="active")  # active|proposed
    set_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BriefSnapshot(Base):
    """The verdict a prior brief saw for one oversight entity's kind —
    the baseline that makes the next brief's deltas computable."""

    __tablename__ = "brief_snapshot"

    site: Mapped[str] = mapped_column(String(200), primary_key=True)
    entity_kind: Mapped[str] = mapped_column(
        String(40), primary_key=True
    )  # node|session|seat|twin|schedule
    entity_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    claim_kind: Mapped[str] = mapped_column(String(80), primary_key=True)
    status: Mapped[str] = mapped_column(String(20))
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CaseVerdict(Base):
    """A decision about a case — a typed decision receipt (ADR-126).

    ``decision_type`` is ``choice``; ``options`` are what could have
    been chosen and ``chosen`` is what was. ``decider_*`` records WHO
    (a human principal here; a governed seat later carries its
    graduation phase). ``stated_confidence`` stays absent for a human
    verdict — ADR-126 D1: an absent confidence renders as information,
    never as a defect.

    ``outcome`` is deliberately nullable and arrives LATER: when the
    case's claims stop being non-green, the open verdict is stamped
    ``resolved``. That later-arriving outcome is what turns a decision
    log into a calibration feed (ADR-126 D3) and what makes precedent
    ("last time we held this, it cleared in 11 minutes") answerable
    from the record instead of from memory.

    Rows are APPEND-ONLY: a change of mind is a new verdict, never an
    edit, so the history a case page shows is the history that happened.
    """

    __tablename__ = "case_verdict"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site: Mapped[str] = mapped_column(String(200), index=True)
    #: stable across polls for the same (site, entity) — see cases.case_id_for
    case_id: Mapped[str] = mapped_column(String(40), index=True)
    decision_type: Mapped[str] = mapped_column(String(20), default="choice")
    options: Mapped[list] = mapped_column(JSON, default=list)
    chosen: Mapped[str] = mapped_column(String(40))
    #: who decided — principal handle + kind (human | rule | model)
    decider: Mapped[str] = mapped_column(String(200))
    decider_kind: Mapped[str] = mapped_column(String(20), default="human")
    #: what the decider was CALLED at decision time. The handle above is
    #: the identity and is usually an IdP subject (a GUID) — right to key
    #: on, wrong to show. Recorded rather than looked up later, because a
    #: directory answers who someone is called today and a decision
    #: record must say who they were called then. Null = not captured
    #: (rows written before this existed); such a row renders by handle.
    decider_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: the case as it stood when the decision was made (title + claim
    #: states), so the record says what was actually in front of them
    evidence: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(String(2000), default="")
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    #: What this decision RAN, if it ran anything. Until now the platform
    #: had nowhere durable to say "this executed, here is what came back":
    #: the audit chain keeps a digest of the inputs and a boolean, the
    #: provenance ledger keeps per-candidate outcomes, and the only place
    #: a return value was written (the approval queue) purges it on
    #: resolution. A decision receipt that cannot say what its action did
    #: is half a receipt.
    ran_capability: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: None = nothing ran. True/False = it ran and this is how it went.
    ran_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: what it returned, or why it did not run — the reader's evidence.
    ran_detail: Mapped[str] = mapped_column(Text, default="")
    #: When a HOLD stops being a hold. "Leave this alone" without an end
    #: is not a decision, it is a way of never deciding: the case simply
    #: vanishes and nothing brings it back. A hold is quiet until this
    #: moment and then the case is open again, which is what makes the
    #: button able to say what it will do.
    hold_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: the later-arriving half (ADR-126 D1)
    outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
