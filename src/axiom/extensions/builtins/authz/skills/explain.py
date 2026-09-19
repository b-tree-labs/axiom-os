# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``audit.explain`` — human-readable rationale for a verdict (PRD §5.4).

Load-bearing surface: PRD requires ``explain`` to cover every
``Verdict.decision``, surfacing the *why* — which rules matched,
their precedence, the graduation state at the moment, and the
federation context.

The output has two parts:

- ``narrative`` — a paragraph-style explanation an operator can paste.
- ``trace``    — the structured breakdown ``narrative`` was built from
                 (matched rules + graduation row + classification +
                 federation hop) so a programmatic consumer can render
                 differently.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..db_models import Graduation, Policy, Verdict
from .list_verdicts import _resolve_session, _to_row


# Precedence per rules.py: deny > propose > require_capability > permit.
_PRECEDENCE = {
    "deny": 0,
    "propose": 1,
    "require_capability": 2,
    "permit": 3,
}


def _intent_class(intent: str) -> str:
    """The ``a.b.c`` → ``a.b`` rollup used by Graduation rows."""
    parts = intent.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else intent


def _rule_to_dict(p: Policy) -> dict[str, Any]:
    return {
        "name": p.name,
        "disposition": p.disposition,
        "priority": p.priority,
        "intent_pattern": p.intent_pattern,
        "actor_pattern": p.actor_pattern,
        "resource_pattern": p.resource_pattern,
        "classification": p.classification,
        "federation_origin_pattern": p.federation_origin_pattern,
        "ttl": p.ttl.isoformat() if p.ttl else None,
    }


def _graduation_to_dict(g: Graduation) -> dict[str, Any]:
    return {
        "actor": g.actor,
        "intent_class": g.intent_class,
        "resource_pattern": g.resource_pattern,
        "approvals": g.approvals,
        "threshold": g.threshold,
        "graduated": g.graduated,
        "last_update": g.last_update.isoformat() if g.last_update else None,
    }


def _build_narrative(
    verdict: Verdict,
    matched_rules: list[Policy],
    graduation: Graduation | None,
    winning_rule_name: str | None,
) -> str:
    decision = verdict.decision
    intent = verdict.intent
    actor = verdict.actor

    lines: list[str] = []
    fed = (
        f" (inbound from federation cohort '{verdict.federation_origin}')"
        if verdict.federation_origin else ""
    )
    lines.append(
        f"At {verdict.decided_at.isoformat() if verdict.decided_at else 'unknown'}, "
        f"GUARD decided **{decision}** for actor {actor!r} "
        f"on intent {intent!r} against resource {verdict.resource!r} "
        f"(classification={verdict.classification}){fed}."
    )

    if matched_rules:
        names = ", ".join(r.name for r in matched_rules)
        lines.append(
            f"Matched {len(matched_rules)} rule(s): {names}."
        )
        if winning_rule_name:
            winner = next(
                (r for r in matched_rules if r.name == winning_rule_name), None
            )
            if winner is not None:
                lines.append(
                    f"Precedence: '{winning_rule_name}' "
                    f"(disposition={winner.disposition}, priority={winner.priority}) "
                    f"determined the verdict. "
                    f"Per spec: deny > propose > require_capability > permit; "
                    f"higher priority wins within a disposition."
                )
    else:
        # No rule matched → either a graduation default or a special path.
        if decision == "propose_to_human":
            if graduation is not None:
                lines.append(
                    f"No explicit rule matched. Graduation for "
                    f"(actor={actor}, intent_class={graduation.intent_class}, "
                    f"resource_pattern={graduation.resource_pattern}) was "
                    f"approvals={graduation.approvals}/{graduation.threshold} "
                    f"(graduated={graduation.graduated}); default disposition "
                    f"is to propose to a human."
                )
            else:
                lines.append(
                    "No explicit rule matched and no graduation row exists for "
                    "this (actor, intent_class) pair — novel action class; "
                    "default disposition is to propose to a human."
                )
        elif decision == "deny":
            lines.append(
                "No explicit rule matched. Default policy is fail-closed: "
                "DENY when nothing matches and graduation cannot promote."
            )
        elif decision == "permit":
            if graduation is not None and graduation.graduated:
                lines.append(
                    f"No explicit rule matched, but graduation for "
                    f"(actor={actor}, intent_class={graduation.intent_class}, "
                    f"resource_pattern={graduation.resource_pattern}) has "
                    f"graduated ({graduation.approvals}/{graduation.threshold} "
                    f"approvals); the action class is autonomous for this actor."
                )
            else:
                lines.append(
                    "No explicit rule matched; permit was the default for this "
                    "decision path."
                )
        elif decision == "rate_limit":
            lines.append(
                "Capability presented at decision time had a rate-limit "
                "threshold; that threshold was exceeded for the actor's recent "
                "history of this intent class."
            )
        elif decision == "expired_capability":
            lines.append(
                f"Capability {verdict.capability_id!r} presented at decision "
                "time was past its TTL; envelope was rejected without rule "
                "evaluation."
            )
        else:
            lines.append(
                f"Decision was {decision}; no rules matched and no other "
                "rationale could be reconstructed."
            )

    lines.append(f"Reason recorded on the verdict: {verdict.reason!r}.")
    return " ".join(lines)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    receipt_id = params.get("receipt_id") or ""
    if not receipt_id:
        return SkillResult(
            ok=False, errors=["receipt_id is required (positional arg)"],
        )

    with _resolve_session(params) as session:
        verdict = (
            session.query(Verdict)
            .filter(Verdict.id == receipt_id)
            .one_or_none()
        )
        if verdict is None:
            return SkillResult(
                ok=False, errors=[f"no verdict found with id={receipt_id!r}"],
            )

        # Look up the matched rules. ``matched_rules`` is a JSON list of
        # rule *names* on the Verdict row.
        rule_names: list[str] = list(verdict.matched_rules or [])
        matched: list[Policy] = []
        if rule_names:
            matched = (
                session.query(Policy)
                .filter(Policy.name.in_(rule_names))
                .all()
            )

        # Pick the precedence winner.
        winner_name: str | None = None
        if matched:
            sorted_matched = sorted(
                matched,
                key=lambda p: (_PRECEDENCE.get(p.disposition, 99), -p.priority),
            )
            winner_name = sorted_matched[0].name

        # Look up the graduation row for this actor + intent class.
        intent_class = _intent_class(verdict.intent)
        grad = (
            session.query(Graduation)
            .filter(
                Graduation.actor == verdict.actor,
                Graduation.intent_class == intent_class,
            )
            .one_or_none()
        )

        narrative = _build_narrative(
            verdict, matched, grad, winner_name
        )

        trace = {
            "verdict": _to_row(verdict),
            "intent_class": intent_class,
            "matched_rules": [_rule_to_dict(r) for r in matched],
            "winning_rule": winner_name,
            "graduation": _graduation_to_dict(grad) if grad else None,
            "capability_id": verdict.capability_id,
            "federation_origin": verdict.federation_origin,
        }

    return SkillResult(
        ok=True,
        value={
            "resource": "explain",
            "receipt_id": receipt_id,
            "narrative": narrative,
            "trace": trace,
        },
    )
