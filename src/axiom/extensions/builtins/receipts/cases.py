# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Cases — the decision unit (design v21: "cases, not receipts").

A case groups today's related non-green claims so the operator decides
ONCE about a situation instead of triaging N rows. This module is the
deterministic server-side grouping the whole surface renders; it
derives everything from the evaluator's items and invents nothing:

- Grouping rule (v1): one case per troubled ENTITY within a site —
  fleet's entity is the node, and a node with three failing claims is
  one situation, not three. (Cross-entity correlation — the same claim
  kind failing everywhere — is a real follow-up and is deliberately
  NOT guessed at here.)
- ``case_id`` is stable across polls for the same (site, entity):
  re-composing while the situation persists names the SAME case, so
  links, chat references, and (later) verdicts attach durably.
- The case's proposal is the union of the items' server-side next
  actions, verbatim. A case with no known next action carries none —
  the surface renders that honestly rather than inventing a plan.
- Severity is the worst member status; ordering is total (severity,
  then the worst member's rank) so two composers agree byte-for-byte.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from axiom.extensions.builtins.receipts.brief import OversightItem, _rank

#: Same attention discipline as the brief (R14): the docket shows at
#: most this many cases; the rest are counted, never hidden.
CASES_CAP = 3

_SEVERITY_RANK = {"failed": 0, "stale": 1, "unknown": 2, "unproven": 3}


@dataclass(frozen=True)
class Case:
    """One decision unit: an entity's non-green claims, grouped."""

    case_id: str
    site: str
    entity_kind: str
    entity_id: str
    title: str
    severity: str
    items: list[OversightItem] = field(default_factory=list)
    proposal: list[str] = field(default_factory=list)
    #: the live decision on this case, if one was made (typed decision
    #: receipt payload); None = nobody has decided yet
    verdict: dict | None = None
    #: the last time this same case was decided AND resolved — precedent
    #: answered from the record, never from memory
    precedent: dict | None = None
    #: the story: which claim explains which (one cause, not N problems).
    #: Composed WITH the case — it needs no store, only the claims.
    cause: dict | None = None
    #: what else this touches, computed from declared edges (R19). None =
    #: not computed for this rendering; a computed-but-empty reach says
    #: "0 declared dependents" in its own summary, never "unknown".
    blast: dict | None = None
    #: how this case is being dealt with, and why a person is seeing it
    #: (receipts.handling). None = not computed for this rendering.
    handling: dict | None = None


def case_id_for(site: str, entity_kind: str, entity_id: str) -> str:
    """Stable id for "the case about this entity at this site"."""
    raw = f"{site}|{entity_kind}|{entity_id}".encode()
    return "c-" + hashlib.sha1(raw).hexdigest()[:8]


def _title(entity_id: str, roots: list) -> str:
    """The headline, in the reader's words.

    The cause chain already knows which claims explain which, so a silent
    node is ONE headline rather than a list of its own symptoms. Where
    the chain finds genuinely independent problems the headline says so
    instead of picking one and hiding the rest.
    """
    from axiom.extensions.builtins.receipts.conditions import condition_for

    if len(roots) == 1:
        root = roots[0]
        return condition_for(root.claim_kind, root.status).title_for(entity_id)
    return f"{entity_id}: {len(roots)} separate problems"


def compose_cases(items: list[OversightItem]) -> list[Case]:
    """Group non-green items into cases (deterministic, derivation-only)."""
    groups: dict[tuple[str, str, str], list[OversightItem]] = {}
    for it in sorted((i for i in items if i.status != "green"), key=_rank):
        groups.setdefault((it.site, it.entity_kind, it.entity_id), []).append(it)

    cases: list[Case] = []
    for (site, ekind, eid), members in groups.items():
        severity = min(members, key=lambda i: _SEVERITY_RANK.get(i.status, 9)).status
        proposal = []
        for m in members:
            if m.next_action and m.next_action not in proposal:
                proposal.append(m.next_action)
        from axiom.extensions.builtins.receipts.cause import explain

        chain = explain(members)
        cases.append(
            Case(
                cause=chain.payload(),
                case_id=case_id_for(site, ekind, eid),
                site=site,
                entity_kind=ekind,
                entity_id=eid,
                title=_title(eid, chain.roots),
                severity=severity,
                items=members,
                proposal=proposal,
            )
        )
    # Total order: worst severity first, then the worst member's rank —
    # so the docket's first card is always the day's worst situation.
    cases.sort(key=lambda c: (_SEVERITY_RANK.get(c.severity, 9), _rank(c.items[0])))
    return cases


def case_payload(case: Case) -> dict:
    """The one wire shape for a case (list + detail alike).

    ``options`` is what a POST to ``/decide`` will accept, sent with every
    case so a surface renders exactly those and invents none. The surface
    used to hardcode one button while the server took two: one real
    option was unreachable, and the button that existed was the surface's
    guess at the server's vocabulary rather than the server's own.
    """

    return {
        "case_id": case.case_id,
        "site": case.site,
        "entity_kind": case.entity_kind,
        "entity_id": case.entity_id,
        "title": case.title,
        "severity": case.severity,
        "items": [vars(i) for i in case.items],
        "proposal": list(case.proposal),
        "verdict": case.verdict,
        "precedent": case.precedent,
        "blast": case.blast,
        "cause": case.cause,
        "handling": case.handling,
        #: How every claim on this case was reached. A FLAT, named list is
        #: what lets one payload serve a per-claim drill-down and a
        #: whole-case audit without either shape winning — the consumption
        #: pattern is not settled, so it is not baked in.
        "derivations": _derivations_for(case),
        #: What a person can do about this case, and what each one will
        #: DO — composed per case, because only the server knows the
        #: declared remedy, the hold window and what this case turns on.
        #: "fix" appears ONLY where a declared fix actually applies, so
        #: the surface can never offer to run something that would not
        #: run. A decided case is offered nothing to decide, but can
        #: still be discussed.
        "options": [o.payload() for o in _offers_for(case)],
    }


def _derivations_for(case: Case) -> list[dict]:
    """Every claim on this case, with how it was reached.

    Collected here rather than rendered anywhere, because the window
    into a resolution has to be the same data in the browser, the CLI,
    the courier and a page somebody prints for a person who was not in
    the room.
    """
    from axiom.extensions.builtins.receipts.derivation import (
        cause_derivation,
        fix_derivation,
        reach_derivation,
    )

    out: list[dict] = []
    for item in case.items:
        if item.derivation:
            out.append(item.derivation)

    cause = case.cause or {}
    steps = cause.get("steps") or []
    roots = [s for s in steps if s.get("is_root")]
    explained = [s["claim_kind"] for s in steps if not s.get("is_root")]
    if len(roots) == 1 and explained:
        out.append(cause_derivation(roots[0]["claim_kind"], explained).payload())

    blast = case.blast or {}
    if blast.get("declared"):
        out.append(reach_derivation(blast.get("summary", ""), blast.get("sources", [])).payload())

    verdict = case.verdict or {}
    if verdict.get("ran_capability"):
        out.append(
            fix_derivation(
                capability=verdict["ran_capability"],
                ok=verdict.get("ran_ok"),
                detail=verdict.get("ran_detail") or "",
                before=verdict.get("evidence") or "",
            ).payload()
        )
    return out


def _offers_for(case: Case) -> list:
    from axiom.extensions.builtins.receipts.offers import offers_for

    return offers_for(case)


def attach_handling(session, cases: list[Case], *, this_node: str = "", **kw) -> list[Case]:
    """Work out, per case, whether anyone needs to be involved.

    Separate from composition and from reach for the same reason the
    others are: grouping is pure, this one reads the decision record, and
    a caller that only needs the list should not pay for either.
    """
    from dataclasses import replace as _replace

    from axiom.extensions.builtins.receipts.handling import handling_for

    return [
        _replace(c, handling=handling_for(session, c, this_node=this_node, **kw).payload())
        for c in cases
    ]


def attach_blast(fleet_session, session, cases: list[Case]) -> list[Case]:
    """Fill each case's reach from declared edges (R19). Kept separate
    from composition and from decisions so each stays independently
    testable — and so a surface can skip the fleet read when it only
    needs the list."""
    from dataclasses import replace as _replace

    from axiom.extensions.builtins.receipts.blast import compute_blast
    from axiom.extensions.builtins.receipts.verdicts import verdicts_for

    out: list[Case] = []
    for case in cases:
        deciders: list[str] = []
        if session is not None:
            # Deduplicate on the HANDLE (the identity) and display the
            # recorded label (the name). Two decisions by one person are
            # one person reached, whatever they were called each time.
            seen: set[str] = set()
            for v in verdicts_for(session, case.case_id, site=case.site):
                if v.decider not in seen:
                    seen.add(v.decider)
                    deciders.append(v.decider_label or v.decider)
        reach = compute_blast(fleet_session, case, prior_deciders=deciders)
        out.append(_replace(case, blast=reach.payload()))
    return out


def attach_decisions(session, cases: list[Case]) -> list[Case]:
    """Fill each case's live verdict and precedent from the decision
    record. Separate from composition on purpose: grouping is pure and
    testable without a store; this is the read that makes the docket
    show what was already decided."""
    from dataclasses import replace as _replace

    from axiom.extensions.builtins.receipts.verdicts import (
        open_verdict,
        precedent_for,
        verdict_payload,
    )

    out: list[Case] = []
    for case in cases:
        live = open_verdict(session, case.case_id, site=case.site)
        out.append(
            _replace(
                case,
                verdict=verdict_payload(live) if live is not None else None,
                precedent=precedent_for(session, case.case_id, site=case.site),
            )
        )
    return out


__all__ = ["CASES_CAP", "Case", "case_id_for", "case_payload", "compose_cases"]
