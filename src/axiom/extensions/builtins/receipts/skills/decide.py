# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``receipts.decide`` — record a decision about a case, and carry it out.

The oversight loop's write verb. The case is recomposed from the
evaluator at decision time so the evidence recorded is what was actually
true then, not what a caller claimed.

``chosen="fix"`` is the difference between a record of judgement and a
supervised operator. It runs the remedy the condition declares, through
:func:`axiom.infra.skill_dispatch.invoke_capability` like every other
surface — so the GUARD consult, the site rules, the audit chain and the
approval hold all apply to it, and a fix is never a private back door
into the platform.

The order is deliberate: RUN FIRST, then record what happened. A verdict
written before the action would be claiming an outcome that had not
occurred yet, and this record's whole value is that it never does that.
A run held for approval is recorded as exactly that — the decision
happened, the action is waiting, and the reader can see both.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..verdicts import OPTIONS


def _run_remedy(case, ctx: SkillContext | None) -> tuple[str, bool, str]:
    """Carry out the declared fix. Returns ``(capability, ok, detail)``.

    Never raises. A decision is recorded either way, and "the fix could
    not run, and here is why" is a result the reader needs rather than an
    error to swallow.
    """
    from axiom.extensions.builtins.receipts.handling import root_condition
    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability
    from axiom.infra.skills import ensure_context

    remedy = root_condition(case).remedy
    if remedy is None:
        return "", False, "No automatic fix is declared for this."

    the_ctx = ensure_context(ctx)
    if not the_ctx.registry.has(remedy.capability):
        # Declared but not registered here. Name the capability, because
        # that is the whole diagnosis.
        return remedy.capability, False, f"{remedy.capability} is not available on this node."

    result = invoke_capability(
        the_ctx.registry,
        remedy.capability,
        dict(remedy.params),
        the_ctx,
        surface=WEB_SURFACE,
    )
    value = result.value if isinstance(result.value, dict) else {}
    if result.ok:
        detail = "; ".join(result.actions_taken) or remedy.summary
        if remedy.proves_done and not value.get(remedy.proves_done):
            # It ran, it did not fail, and it did nothing. Recording that
            # as a successful fix would tell a person the situation was
            # handled when the platform knows it was not.
            return remedy.capability, False, detail
        return remedy.capability, True, detail
    if value.get("status") == "pending_approval":
        return remedy.capability, False, "The fix is waiting for approval before it can run."
    return remedy.capability, False, "; ".join(result.errors) or "the fix did not run"


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    case_id = str(params.get("case_id") or "").strip()
    chosen = str(params.get("chosen") or "").strip()
    decider = str(params.get("decider") or "").strip()
    # What to SHOW for the decider. Optional: a caller that knows no
    # display name records none, and the surface falls back to the
    # handle rather than to a blank.
    decider_label = str(params.get("decider_label") or "").strip()
    note = str(params.get("note") or "")
    site = params.get("site") or None

    if not case_id:
        return SkillResult(ok=False, errors=["case_id is required"])
    if chosen not in OPTIONS:
        return SkillResult(ok=False, errors=[f"chosen must be one of {', '.join(OPTIONS)}"])
    if not decider:
        return SkillResult(
            ok=False,
            errors=["decider is required — an unattributed decision is not a record"],
        )

    from axiom.extensions.builtins.fleet import store as fleet_store

    from .. import store
    from ..brief import fleet_source
    from ..cases import compose_cases
    from ..verdicts import record_verdict, verdict_payload

    sites = [site] if site else None
    with fleet_store.session_scope() as fsession:
        items = fleet_source(fsession, sites=sites)
    case = next((c for c in compose_cases(items) if c.case_id == case_id), None)
    if case is None:
        # Deciding about a situation the evaluator no longer reports would
        # record evidence that is not true — refuse rather than invent.
        return SkillResult(ok=False, errors=[f"no live case {case_id} — nothing to decide about"])

    ran_capability, ran_ok, ran_detail = "", None, ""
    if chosen == "fix":
        ran_capability, ran_ok, ran_detail = _run_remedy(case, ctx)

    # A hold has an end. "Leave this alone" with no end is a way of never
    # deciding: the case vanishes and nothing brings it back.
    hold_until = None
    if chosen == "hold":
        from datetime import UTC, datetime

        from ..verdicts import HOLD_WINDOW

        hold_until = datetime.now(UTC) + HOLD_WINDOW

    with store.session_scope() as session:
        verdict = record_verdict(
            session,
            case,
            chosen=chosen,
            decider=decider,
            decider_label=decider_label,
            note=note,
            hold_until=hold_until,
            ran_capability=ran_capability,
            ran_ok=ran_ok,
            ran_detail=ran_detail,
        )
        session.commit()
        payload = verdict_payload(verdict)

    taken = [f"recorded {chosen} on case {case_id} by {decider_label or decider}"]
    if ran_capability:
        taken.append(f"ran {ran_capability}: {'ok' if ran_ok else 'did not run'}")
    return SkillResult(ok=True, value={"case_id": case_id, "verdict": payload}, actions_taken=taken)


__all__ = ["run"]
