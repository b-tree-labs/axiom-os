# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The decision record: what we decided about a case, and whether it worked.

This is the oversight loop's second half. A status board shows what is
broken; the decision record answers "what did we decide, who decided
it, on what evidence, and did it work" — which is what makes the same
situation cheaper the second time.

Every verdict is a typed decision receipt (ADR-126 D1): decision_type
``choice``, the ``options`` that existed, the ``chosen`` one, the
``decider``, the ``evidence`` as it stood, and an ``outcome`` that
arrives LATER. Human verdicts carry no stated_confidence — its absence
is information, not a defect (ADR-126 D1).

Append-only: a change of mind is a new verdict. The history a case page
shows is the history that happened.

Outcome stamping is derived, never asserted: when a case's claims are
no longer non-green, its open verdicts are stamped ``resolved`` with
the timestamp; if the situation is still live, nothing is stamped. So
"held, cleared in 11 minutes" is a measurement, not a claim.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.receipts.cases import Case
from axiom.extensions.builtins.receipts.db_models import CaseVerdict

#: What a person may decide about a case today. Each option is an act the
#: platform can actually carry out or record — no option exists here that
#: the surface cannot honour (no dead affordances, server side).
OPTIONS = ("fix", "hold", "acknowledge")

#: Options that are always available on an undecided case. "fix" is NOT
#: here: it appears only when a declared fix actually applies, because an
#: option the platform cannot carry out is a dead affordance, and the
#: first rule of this surface is that nothing renders that the platform
#: did not actually do.
ALWAYS_OFFERED = ("hold", "acknowledge")

#: Outcomes, stamped by observation (never by assertion).
OUTCOME_RESOLVED = "resolved"

#: How long a hold holds. A fixed, explainable window rather than a
#: setting, because the button has to be able to say what it will do and
#: "quiet until whatever someone configured" is not a sentence a person
#: can act on. When it lapses the case is open again on the next compose.
HOLD_WINDOW = timedelta(hours=24)


#: What a decision READS as once taken. The surface has the same map;
#: this one exists because a profile sentence ("3 held, 1 fixed") is
#: composed here, and a second spelling of the same choice would be two
#: vocabularies for one record.
_TAKEN = {"fix": "Fixed", "hold": "Held", "acknowledge": "Acknowledged"}


def decision_taken_label(chosen: str) -> str:
    return _TAKEN.get(chosen, chosen.capitalize())


def _evidence_of(case: Case) -> str:
    """What was in front of the decider, in one line, recorded verbatim."""
    claims = "; ".join(f"{i.claim_kind} is {i.status}" for i in case.items)
    return f"{case.title} — {claims}"


def record_verdict(
    session,
    case: Case,
    *,
    chosen: str,
    decider: str,
    decider_kind: str = "human",
    decider_label: str = "",
    note: str = "",
    hold_until: datetime | None = None,
    ran_capability: str = "",
    ran_ok: bool | None = None,
    ran_detail: str = "",
    now: datetime | None = None,
) -> CaseVerdict:
    """Append one decision about ``case``. Caller commits."""
    if chosen not in OPTIONS:
        raise ValueError(f"chosen must be one of {OPTIONS}, got {chosen!r}")
    if not decider:
        raise ValueError(
            "a verdict must name its decider — an unattributed decision is not a record"
        )
    verdict = CaseVerdict(
        site=case.site,
        case_id=case.case_id,
        decision_type="choice",
        options=list(OPTIONS),
        chosen=chosen,
        decider=decider,
        decider_kind=decider_kind,
        # Empty stays NULL: "not captured" is a fact, and a blank string
        # would render as a nameless decider rather than fall back to
        # the handle.
        decider_label=decider_label or None,
        evidence=_evidence_of(case),
        note=note[:2000],
        hold_until=hold_until,
        ran_capability=ran_capability or None,
        ran_ok=ran_ok,
        ran_detail=ran_detail[:8000],
        decided_at=now or datetime.now(UTC),
    )
    session.add(verdict)
    return verdict


def verdicts_for(session, case_id: str, *, site: str = "") -> list[CaseVerdict]:
    """This case's decisions, newest first."""
    q = session.query(CaseVerdict).filter(CaseVerdict.case_id == case_id)
    if site:
        q = q.filter(CaseVerdict.site == site)
    return list(q.order_by(CaseVerdict.decided_at.desc(), CaseVerdict.id.desc()))


def _aware(value: datetime | None) -> datetime | None:
    """SQLite gives a stored timestamp back without its timezone, while
    Postgres keeps it. Comparing the two raises, so the naive one is read
    as UTC — which is what was written."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def is_live(verdict: CaseVerdict, *, now: datetime | None = None) -> bool:
    """Is this decision still the standing answer?

    A hold that has lapsed is not. That is the whole point of giving a
    hold an end: the case comes back rather than disappearing for good.
    """
    if verdict.outcome is not None:
        return False
    until = _aware(verdict.hold_until)
    if verdict.chosen == "hold" and until is not None:
        return (now or datetime.now(UTC)) < until
    return True


def open_verdict(
    session, case_id: str, *, site: str = "", now: datetime | None = None
) -> CaseVerdict | None:
    """The decision standing on this case right now, if any."""
    for v in verdicts_for(session, case_id, site=site):
        if is_live(v, now=now):
            return v
    return None


def stamp_outcomes(
    session,
    live_case_ids: set[str],
    *,
    site: str = "",
    now: datetime | None = None,
) -> list[CaseVerdict]:
    """Close out decisions whose situations are gone.

    ``live_case_ids`` is what the evaluator STILL reports as troubled.
    Any open verdict whose case is absent from that set has been
    resolved — observed, not asserted. Returns what was stamped so a
    caller can render "resolved" trust changes. Caller commits.
    """
    when = now or datetime.now(UTC)
    q = session.query(CaseVerdict).filter(CaseVerdict.outcome.is_(None))
    if site:
        q = q.filter(CaseVerdict.site == site)
    stamped: list[CaseVerdict] = []
    for verdict in q.all():
        if verdict.case_id in live_case_ids:
            continue
        verdict.outcome = OUTCOME_RESOLVED
        verdict.outcome_at = when
        stamped.append(verdict)
    return stamped


def verdict_payload(verdict: CaseVerdict) -> dict:
    """One wire shape for a decision (list, detail and precedent alike)."""
    minutes = None
    if verdict.outcome_at is not None and verdict.decided_at is not None:
        minutes = round((verdict.outcome_at - verdict.decided_at).total_seconds() / 60.0, 1)
    return {
        "decision_type": verdict.decision_type,
        "options": list(verdict.options or []),
        "chosen": verdict.chosen,
        "decider": verdict.decider,
        "decider_kind": verdict.decider_kind,
        #: what to SHOW; the handle above stays the identity. Null when
        #: the decision predates label capture — render the handle then.
        "decider_label": verdict.decider_label,
        "evidence": verdict.evidence,
        "note": verdict.note,
        #: when a hold lapses; null = held with no end (older rows)
        "hold_until": verdict.hold_until.isoformat() if verdict.hold_until else None,
        #: what this decision actually ran. None = it ran nothing, which
        #: is a different fact from having run and failed.
        "ran_capability": verdict.ran_capability,
        "ran_ok": verdict.ran_ok,
        "ran_detail": verdict.ran_detail,
        "decided_at": verdict.decided_at.isoformat() if verdict.decided_at else None,
        "outcome": verdict.outcome,
        "outcome_at": verdict.outcome_at.isoformat() if verdict.outcome_at else None,
        #: how long the situation took to clear after this decision —
        #: the number precedent is made of.
        "minutes_to_outcome": minutes,
    }


def precedent_for(session, case_id: str, *, site: str = "") -> dict | None:
    """The last time this same case was decided AND resolved.

    Case ids are stable across polls for the same (site, entity), so a
    recurrence names the same case — which is exactly what makes "we
    have seen this before, here is what worked" answerable from the
    record instead of from memory.
    """
    for v in verdicts_for(session, case_id, site=site):
        if v.outcome == OUTCOME_RESOLVED:
            return verdict_payload(v)
    return None


__all__ = [
    "ALWAYS_OFFERED",
    "HOLD_WINDOW",
    "is_live",
    "decision_taken_label",
    "OPTIONS",
    "OUTCOME_RESOLVED",
    "open_verdict",
    "precedent_for",
    "record_verdict",
    "stamp_outcomes",
    "verdict_payload",
    "verdicts_for",
]
