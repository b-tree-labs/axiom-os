# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The brief composer — ONE deterministic renderer for CLI, MCP and web.

Positioning (PRD, founder 2026-09-24): agent oversight with resolution.
The brief renders the loop, not the log: adjudicable claims and
decidable windows only; verified-as-expected activity compresses to a
single line; the itemized band is TRUST DELTAS — authority and verdicts
changing hands — never activity.

Courier doctrine (R16): the output here is composed server-side and is
byte-stable for a given store state and ``now``. A harness agent
relaying it adds nothing and can corrupt nothing. Any model
participation beyond this projection is a separately governed
escalation seat — never this module's business.

The item model is the OVERSIGHT plane's, not fleet's: every supervised
entity (node, session, seat, schedule, twin) contributes
``OversightItem``s through a source. Fleet is the first wired source;
the seams for the others are the ``sources`` parameter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from axiom.extensions.builtins.receipts.conditions import condition_for
from axiom.extensions.builtins.receipts.db_models import BriefSnapshot, Focus

# R14: the hard attention budget. A brief that can grow unboundedly is
# a feed, and feeds die.
NEEDS_YOU_CAP = 3

# Deterministic consequence order for needs-you ranking (worst first),
# then kind priority, then entity id — total order, no ties.
_STATUS_RANK = {"failed": 0, "stale": 1, "unknown": 2, "unproven": 3}
_KIND_PRIORITY = ["backup", "canary", "heartbeat", "service_health", "versions"]


@dataclass(frozen=True)
class OversightItem:
    """One supervised entity's adjudicable claim (or decidable window)."""

    entity_kind: str  # node | session | seat | schedule | twin
    entity_id: str
    claim_kind: str
    status: str  # the honesty taxonomy value, server-computed
    evidence: str
    next_action: str | None = None
    site: str = ""
    #: when the report behind this verdict arrived (ISO, server-side).
    #: The cause chain needs order; a surface that shows "since when"
    #: must not compute it from the client's clock.
    observed_at: str = ""
    #: how this claim was reached — stored values, rule, how to check it
    #: independently, and what it cannot establish. See receipts.derivation.
    derivation: dict | None = None


@dataclass(frozen=True)
class Brief:
    generated_at: str
    site: str | None
    focus: dict | None
    needs_you: list[OversightItem]
    trust_deltas: list[dict]
    quiet_line: str
    counts: dict = field(default_factory=dict)
    not_yet_wired: list[str] = field(default_factory=list)
    #: The decision units (design v21): non-green items grouped per
    #: troubled entity, capped by the same attention discipline as
    #: needs_you. Composed in compose_brief; see receipts.cases.
    cases: list = field(default_factory=list)
    #: Cases somebody has already decided, most recent decision first.
    #: Not what needs you — which is why they are not in ``cases`` — but
    #: the record of what was decided, which Decide exists to show.
    decided_cases: list = field(default_factory=list)


# Server-side next actions (PRD R13) now come from the ONE condition
# table (receipts.conditions), which also supplies the plain-language
# headline and detail. There used to be a second table here holding shell
# runbooks — "systemctl --user list-timers axiom-fleet-report.timer
# (Linux) / launchctl list | grep fleet-report (macOS)" — pasted into a
# sentence a person was expected to read. A command someone has to retype
# is not a next action; it is a runbook wearing one.


def _rank(item: OversightItem) -> tuple:
    kind_rank = (
        _KIND_PRIORITY.index(item.claim_kind)
        if item.claim_kind in _KIND_PRIORITY
        else len(_KIND_PRIORITY)
    )
    return (
        _STATUS_RANK.get(item.status, 9),
        kind_rank,
        item.entity_kind,
        item.entity_id,
        item.claim_kind,
    )


def fleet_source(fleet_session, *, sites=None, now=None) -> list[OversightItem]:
    """The first oversight source: the fleet evaluator's verdicts,
    verbatim (statuses are computed there, never here)."""
    from axiom.extensions.builtins.fleet.view import fleet_status

    out: list[OversightItem] = []
    view = fleet_status(fleet_session, sites=sites, now=now)
    for node in view["nodes"]:
        for kind, v in sorted(node["kinds"].items()):
            out.append(
                OversightItem(
                    entity_kind="node",
                    entity_id=node["node_id"],
                    claim_kind=kind,
                    status=v["status"],
                    evidence=v["evidence"],
                    next_action=condition_for(kind, v["status"]).fix or None,
                    derivation=v.get("derivation"),
                    site=node["site"],
                    observed_at=str(v.get("received_at") or ""),
                )
            )
    return out


def compose_brief(
    receipts_session,
    items: list[OversightItem],
    *,
    site: str | None = None,
    now: datetime | None = None,
    snapshot: bool = True,
    this_node: str = "",
    fleet_session=None,
) -> Brief:
    """Compose the budgeted, delta-honest brief from oversight items.

    Deterministic for a given (items, stored snapshot, focus, now).
    ``snapshot=True`` records the current verdicts as the next brief's
    baseline AFTER computing deltas (caller commits).
    """
    now = now or datetime.now(UTC)
    scope = site or ""

    # --- deltas against the recorded baseline (R14: deltas, never states)
    prior = {
        (r.entity_kind, r.entity_id, r.claim_kind): r.status
        for r in receipts_session.query(BriefSnapshot).filter_by(site=scope).all()
    }
    deltas = []
    for it in sorted(items, key=_rank):
        before = prior.get((it.entity_kind, it.entity_id, it.claim_kind))
        if before is not None and before != it.status:
            deltas.append(
                {
                    "entity_kind": it.entity_kind,
                    "entity_id": it.entity_id,
                    "claim_kind": it.claim_kind,
                    "from": before,
                    "to": it.status,
                    "evidence": it.evidence,
                }
            )

    # --- needs-you: non-green, budgeted, totally ordered
    non_green = [i for i in items if i.status != "green"]
    needs = sorted(non_green, key=_rank)[:NEEDS_YOU_CAP]
    overflow = len(non_green) - len(needs)

    # --- cases: the decision units (grouped, stable ids, capped)
    from axiom.extensions.builtins.receipts.cases import (
        CASES_CAP,
        attach_decisions,
        compose_cases,
    )
    from axiom.extensions.builtins.receipts.verdicts import stamp_outcomes

    all_cases = compose_cases(items)
    # Outcomes are OBSERVED, not asserted: a decision whose case the
    # evaluator no longer reports has been resolved, and gets stamped
    # with when. That later-arriving half is what makes precedent and
    # calibration real (ADR-126 D1/D3). Only when this composition
    # records (snapshot=True) — a read-only web poll never writes.
    if snapshot:
        stamp_outcomes(receipts_session, {c.case_id for c in all_cases}, site=scope, now=now)
    all_cases = attach_decisions(receipts_session, all_cases)
    # The brief is what NEEDS somebody. A case that has already been
    # decided is not that: the decision is on the record, and putting it
    # back in the list asks the same question twice. Decided cases are
    # counted in the quiet line rather than silently dropped.
    # attach_decisions only reports a verdict that is still STANDING, so
    # a lapsed hold has already dropped out and its case is open again.
    open_cases = [c for c in all_cases if not c.verdict]
    decided_cases = [c for c in all_cases if c.verdict]
    decided = len(decided_cases)
    if this_node:
        from axiom.extensions.builtins.receipts.cases import attach_handling

        open_cases = attach_handling(receipts_session, open_cases, this_node=this_node)
    shown_cases = open_cases[:CASES_CAP]
    # Reach comes with the list, not only the drill-down. A card that
    # offers to run something while saying nothing about what it touches
    # is asking for a decision nobody has the standing to make. Bounded
    # by the same cap, so this is at most CASES_CAP entity reads.
    if fleet_session is not None and shown_cases:
        from axiom.extensions.builtins.receipts.cases import attach_blast

        shown_cases = attach_blast(fleet_session, receipts_session, shown_cases)

    green = [i for i in items if i.status == "green"]
    counts = {
        "green": len(green),
        "non_green": len(non_green),
        "needs_you_shown": len(needs),
        "needs_you_overflow": max(0, overflow),
        "deltas": len(deltas),
        "cases": len(open_cases),
        #: decided, therefore not shown. Counted so the drop is visible.
        "cases_decided": decided,
        "cases_overflow": max(0, len(open_cases) - len(shown_cases)),
    }
    # Plain words, and grammatical at one. "1 claims verified quietly"
    # was shipping; pluralize() has been in axiom.infra.text_utils the
    # whole time, unconsulted.
    from axiom.infra.text_utils import pluralize

    if not items:
        quiet = "Nothing is reporting in here yet."
    elif not non_green and not deltas:
        quiet = f"{pluralize(len(green), 'check')} passed, and nothing you rely on changed."
    else:
        quiet = f"{pluralize(len(green), 'check')} passed quietly."
    if decided:
        quiet = f"{pluralize(decided, 'case')} already decided, and {quiet[0].lower()}{quiet[1:]}"

    focus_row = receipts_session.get(Focus, scope)
    focus = (
        {
            "text": focus_row.text,
            "set_by": focus_row.set_by,
            "state": focus_row.state,
            "set_at": focus_row.set_at.isoformat(),
        }
        if focus_row
        else None
    )

    if snapshot:
        for r in receipts_session.query(BriefSnapshot).filter_by(site=scope).all():
            receipts_session.delete(r)
        for it in items:
            receipts_session.add(
                BriefSnapshot(
                    site=scope,
                    entity_kind=it.entity_kind,
                    entity_id=it.entity_id,
                    claim_kind=it.claim_kind,
                    status=it.status,
                    taken_at=now,
                )
            )

    return Brief(
        generated_at=now.isoformat(),
        site=site,
        focus=focus,
        needs_you=needs,
        trust_deltas=deltas,
        quiet_line=quiet,
        counts=counts,
        # Honesty about the plane's coverage: sources not yet wired are
        # named, not implied (sessions/seats/twins arrive with F1/F3).
        not_yet_wired=["session", "seat", "schedule", "twin"],
        cases=shown_cases,
        # Decide is the decision record, so it needs what was decided as
        # well as what is open. Same cap: a list nobody can read is not a
        # record anybody consults.
        decided_cases=decided_cases[:CASES_CAP],
    )


# Internal source ids get plain display names on every surface
# (terminology ledger: internal-only terms never render). One map,
# consumed by the text projection and the structured payload alike.
PLAIN_SOURCE_NAMES = {
    "session": "agent sessions",
    "seat": "decision roles",
    "schedule": "schedules",
    "twin": "twins",
}


def _case_payload(case) -> dict:
    from axiom.extensions.builtins.receipts.cases import case_payload

    return case_payload(case)


def brief_payload(brief: Brief) -> dict:
    """The structured projection of the brief — the ONE payload shape the
    skill result, the MCP structured form, and ``/api/v1/receipts/today``
    all carry. Deterministic for a given ``Brief``; sources arrive
    already mapped to plain display names."""
    return {
        "generated_at": brief.generated_at,
        "site": brief.site,
        "focus": brief.focus,
        "needs_you": [vars(i) for i in brief.needs_you],
        "trust_deltas": brief.trust_deltas,
        "quiet_line": brief.quiet_line,
        "counts": brief.counts,
        "not_yet_wired": [PLAIN_SOURCE_NAMES.get(k, k) for k in brief.not_yet_wired],
        "cases": [_case_payload(c) for c in brief.cases],
        "decided_cases": [_case_payload(c) for c in brief.decided_cases],
    }


def render_brief_text(brief: Brief) -> str:
    """The byte-stable text projection every courier consumer relays."""
    lines: list[str] = []
    lines.append(f"TODAY — oversight brief · {brief.generated_at}")
    if brief.focus:
        marker = "" if brief.focus["state"] == "active" else " (PROPOSED, unconfirmed)"
        lines.append(f"FOCUS{marker}: {brief.focus['text']}  — set by {brief.focus['set_by']}")
    if brief.needs_you:
        lines.append(
            f"NEEDS YOU ({len(brief.needs_you)} shown"
            + (
                f", {brief.counts['needs_you_overflow']} more in drill-down"
                if brief.counts.get("needs_you_overflow")
                else ""
            )
            + "):"
        )
        for it in brief.needs_you:
            # FAILED means the evidence contradicts the claim — in the
            # standard vocabulary (terminology ledger), a counterexample.
            label = "counterexample: " if it.status == "failed" else ""
            lines.append(
                f"  [{it.status.upper()}] {it.entity_kind} {it.entity_id} · {it.claim_kind} — {label}{it.evidence}"
            )
            if it.next_action:
                lines.append(f"      next: {it.next_action}")
    if brief.trust_deltas:
        lines.append("TRUST DELTAS:")
        for d in brief.trust_deltas:
            lines.append(
                f"  {d['entity_kind']} {d['entity_id']} · {d['claim_kind']}: "
                f"{d['from'].upper()} → {d['to'].upper()} — {d['evidence']}"
            )
    lines.append(brief.quiet_line)
    if brief.not_yet_wired:
        shown = ", ".join(PLAIN_SOURCE_NAMES.get(k, k) for k in brief.not_yet_wired)
        lines.append(f"(not yet watched here: {shown})")
    return "\n".join(lines)
