# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The runtime half of the chaining layer (ADR-185).

One wrapper, applied at the dispatch chokepoint
(:func:`axiom.infra.skill_dispatch.invoke_capability`) and by the one surface
that dispatches beneath it (chat). It does four things around a skill and
nothing inside it:

1. **Preconditions.** The declared ``requires`` are evaluated before the skill
   runs. Unmet ones come back as a structured refusal,
   ``{unmet: [{id, why, fix}]}``, never an exception, and the skill body does
   not run.
2. **Plan / apply for destructive skills.** A ``risk="destructive"`` skill
   refuses without a ``plan_hash`` that names an *approved* plan. The first call
   opens the plan on the existing approval queue and returns the hash; the apply
   call carries it back. The hash binds the approval to exactly these
   parameters, and an approval is single-use.
3. **The envelope.** After the skill ran, the result gains one additive,
   versioned ``chain`` key: ``next`` (declared follow-ups that are actually
   available here, plus any the skill returned) and ``produced`` (the typed refs
   this result carries). Existing result fields are never altered.
4. **Chain context.** A caller may pass ``_chain = {chain_id, parent_step}``;
   the wrapper mints a ``step_id`` and writes all three into the action audit
   record, so a sequence of calls is reconstructable from the ledger that
   already exists. No new store.

Nothing here makes one skill depend on another. A skill with no declarations
runs exactly as it did before.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from axiom.infra.skill_declarations import (
    APPROVAL_PARAM,
    CHAIN_ARG,
    NextStep,
    check_precondition,
    coerce_next,
    fill_fix,
)

if TYPE_CHECKING:
    from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec

log = logging.getLogger(__name__)

#: Surfaces where a person is already at the verb (a terminal, or a runner
#: executing what a person approved). A destructive skill is not plan-gated
#: there: the CLI skills confirm themselves, and the runner IS the apply half.
#: Every other surface (an MCP client, a chat turn, a browser) acts for someone
#: who is not at the verb, so it plans and waits.
UNGATED_SURFACES: frozenset[str] = frozenset({"cli", "runner"})

#: Follow-ups carried in one envelope. Bounded because the envelope rides every
#: result into a context window.
MAX_NEXT = 5
MAX_PRODUCED = 6

_CHAIN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_SCALARS = (str, int, float, bool)

#: Which exposure flag lets a follow-up be offered on which surface.
_EXPOSURE_FOR_SURFACE = {"mcp": "mcp", "chat": "agent_tool", "cli": "cli"}


# ---------------------------------------------------------------- chain context


def split_chain_arg(arguments: Mapping[str, Any] | None) -> tuple[dict[str, Any], dict[str, str] | None]:
    """Separate the reserved ``_chain`` argument from a transport's arguments.

    Returns ``(arguments_without_it, chain)``. A malformed ``_chain`` is dropped
    rather than refused: it is bookkeeping, and bookkeeping must not fail a call.
    """
    args = dict(arguments or {})
    raw = args.pop(CHAIN_ARG, None)
    return args, normalise_chain(raw)


def normalise_chain(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, Mapping):
        return None
    chain_id = str(raw.get("chain_id") or "")
    if not _CHAIN_ID.match(chain_id):
        return None
    out = {"chain_id": chain_id}
    parent = str(raw.get("parent_step") or "")
    if parent and _CHAIN_ID.match(parent):
        out["parent_step"] = parent
    return out


# ---------------------------------------------------------------- preconditions


def unmet_preconditions(spec: SkillSpec | None, params: dict[str, Any], ctx: Any) -> list[dict[str, str]]:
    """Every declared precondition this call does not meet, as ``{id, why, fix}``."""
    if spec is None or not spec.requires:
        return []
    out: list[dict[str, str]] = []
    for pre in spec.requires:
        met, why = check_precondition(pre, params, ctx)
        if not met:
            out.append(
                {
                    "id": pre.id,
                    "why": why or pre.description,
                    "fix": fill_fix(pre.fix, params),
                }
            )
    return out


# ---------------------------------------------------------------- plan / apply


def plan_digest(capability: str, params: Mapping[str, Any] | None) -> str:
    """The hash an approval binds to: this capability with exactly these parameters.

    ``plan_hash`` itself, the reserved chain argument and any ``_``-prefixed key
    (a hook injected by a test or a caller, never a user-visible parameter) are
    left out, so the plan and its apply compute the same value. Not redacted:
    this is a digest that never leaves the process in the clear, and redacting
    would make two different secret values plan identically.
    """
    body = {
        k: v
        for k, v in dict(params or {}).items()
        if k != APPROVAL_PARAM and not str(k).startswith("_")
    }
    canonical = json.dumps(
        {"capability": capability, "params": body}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _approvals_path(ctx: Any) -> Path:
    state_dir = getattr(ctx, "state_dir", None)
    if state_dir:
        return Path(state_dir) / "orchestrator" / "approvals.json"
    from axiom.infra.orchestrator.approval_store import default_approval_path

    return default_approval_path()


def _gate_for(ctx: Any):
    from axiom.infra.orchestrator.approval import ApprovalGate
    from axiom.infra.orchestrator.approval_store import FileActionStore

    return ApprovalGate(FileActionStore(_approvals_path(ctx)))


def _plan_marker(plan_hash: str) -> str:
    return f"plan_hash={plan_hash};"


def _find_plan(gate: Any, capability: str, plan_hash: str) -> Any | None:
    """The open (pending or approved) plan for this hash.

    A plan that was applied, rejected, failed or expired is spent: finding it
    again would strand a retry of the same call behind an approval that can no
    longer be used, so only an open one counts and the caller plans anew.
    """
    marker = _plan_marker(plan_hash)
    for action in gate.all_actions():
        if (
            action.name == capability
            and (action.reason or "").startswith(marker)
            and action.status.value in ("pending", "approved")
        ):
            return action
    return None


def _cli_name() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


@dataclass
class Prepared:
    """What :func:`prepare` decided before the skill ran."""

    ctx: Any
    params: dict[str, Any]
    refusal: SkillResult | None = None
    claimed: Any | None = None  # the approved plan action this call is applying
    gate: Any | None = None
    step_id: str = ""
    chain: dict[str, str] | None = None


def _plan_refusal(
    capability: str, params: dict[str, Any], ctx: Any, *, supplied: str | None
) -> tuple[list[dict[str, str]], list[dict[str, Any]], dict[str, Any] | None, Any | None]:
    """Open (or find) the plan for a destructive call and say what is outstanding.

    Returns ``(unmet, produced, value, claimed)``; ``claimed`` is the approved
    plan action when the supplied hash names one that may be applied now.
    """
    expected = plan_digest(capability, params)
    cli = _cli_name()
    try:
        gate = _gate_for(ctx)
        action = _find_plan(gate, capability, expected)
        if action is None:
            from axiom.infra.orchestrator.actions import Action, ActionCategory

            action = Action(
                name=capability,
                params={k: v for k, v in params.items() if k != APPROVAL_PARAM and not str(k).startswith("_")},
                category=ActionCategory.WRITE,
                reason=f"{_plan_marker(expected)} destructive call awaiting approval",
            )
            action = gate.submit(action)
    except Exception as exc:  # noqa: BLE001 - an unreachable queue is reported, not raised
        log.warning("could not open a plan for %s: %s", capability, exc)
        return (
            [
                {
                    "id": "approval-queue",
                    "why": "the approval queue could not be written, so no plan can be opened",
                    "fix": f"make {_approvals_path(ctx)} writable, then call {capability} again",
                }
            ],
            [],
            None,
            None,
        )

    produced = [
        {
            "kind": APPROVAL_PARAM,
            "value": expected,
            "how_to_use": f"pass as {APPROVAL_PARAM} to {capability}, with the same other parameters, once the plan is approved",
        }
    ]
    value = {"plan_hash": expected, "held_action_id": action.action_id, "status": action.status.value}
    status = action.status.value
    if supplied and supplied != expected:
        return (
            [
                {
                    "id": "plan-matches-call",
                    "why": "the plan_hash supplied does not belong to these parameters; the call changed after it was planned",
                    "fix": f"use plan_hash={expected} with exactly these parameters, or call {capability} without plan_hash to plan again",
                }
            ],
            produced,
            value,
            None,
        )
    if status == "approved" and supplied == expected:
        return [], produced, value, action
    if status == "approved":  # approved, but the caller has not presented the hash
        return (
            [
                {
                    "id": "approved-plan",
                    "why": "the plan is approved but this call did not present its plan_hash",
                    "fix": f"call {capability} again with plan_hash={expected}",
                }
            ],
            produced,
            value,
            None,
        )
    if status == "pending":
        return (
            [
                {
                    "id": "approved-plan",
                    "why": f"{capability} is destructive and its plan {action.action_id} has not been approved",
                    "fix": (
                        f"a person approves it with '{cli} approve ok {action.action_id}', "
                        f"then call {capability} again with plan_hash={expected}"
                    ),
                }
            ],
            produced,
            value,
            None,
        )
    return (  # unreachable for an open plan; kept so a new status cannot slip through
        [{"id": "approved-plan", "why": f"plan {action.action_id} is {status}", "fix": f"call {capability} without plan_hash to plan again"}],
        produced,
        value,
        None,
    )


def prepare(
    registry: SkillRegistry,
    capability: str,
    params: dict[str, Any],
    ctx: SkillContext | None,
    *,
    surface: str,
    chain: dict[str, str] | None = None,
) -> Prepared:
    """Everything the wrapper decides before the skill body may run."""
    from axiom.infra.skills import SkillResult, ensure_context

    spec = registry.spec(capability)
    prepared = Prepared(ctx=ctx, params=params)

    if chain:
        ctx = ensure_context(ctx)
        step_id = uuid.uuid4().hex[:12]
        stamped = {**chain, "step_id": step_id}
        ctx = replace(ctx, chain=stamped)
        prepared.ctx = ctx
        prepared.step_id = step_id
        prepared.chain = stamped

    if spec is None:
        return prepared  # a legacy registration declares nothing

    unmet = unmet_preconditions(spec, params, prepared.ctx)
    produced: list[dict[str, Any]] = []
    value: dict[str, Any] | None = None

    if not unmet and spec.effective_risk == "destructive" and surface not in UNGATED_SURFACES:
        supplied = str(params.get(APPROVAL_PARAM) or "") or None
        unmet, produced, value, claimed = _plan_refusal(capability, params, prepared.ctx, supplied=supplied)
        if not unmet:
            prepared.claimed = claimed
            gate = _gate_for(prepared.ctx)
            fresh = gate.get(claimed.action_id)
            # Compare-and-set, exactly as the runner claims work: the approval is
            # spent before the skill runs, so two callers holding one hash cannot
            # both apply it.
            if fresh is None or fresh.status.value != "approved":
                unmet = [
                    {
                        "id": "plan-unused",
                        "why": "the approval was spent by another call a moment ago",
                        "fix": f"plan again by calling {capability} without plan_hash",
                    }
                ]
            else:
                from axiom.infra.orchestrator.actions import ActionStatus

                fresh.status = ActionStatus.RUNNING
                gate.record(fresh)
                prepared.claimed = fresh
                prepared.gate = gate

    if unmet:
        errors = [f"{capability} did not run: {u['id']}: {u['why']}. Fix: {u['fix']}" for u in unmet]
        refusal = SkillResult(ok=False, value=value, errors=errors, unmet=unmet, produced=produced)
        if prepared.chain:
            refusal.chain = {k: v for k, v in prepared.chain.items() if v}
        if spec.effective_risk != "read":
            _audit_refusal(capability, params, prepared.ctx, unmet)
        prepared.refusal = refusal
    return prepared


def _audit_refusal(capability: str, params: dict[str, Any], ctx: Any, unmet: list[dict[str, str]]) -> None:
    if ctx is None or getattr(ctx, "state_dir", None) is None:
        return
    try:
        from axiom.infra import audit_trail

        audit_trail.emit(ctx, skill=capability, params=params, outcome="refused", errors_count=len(unmet))
    except Exception:  # noqa: BLE001 - the ledger never fails a refusal
        log.debug("refusal not audited", exc_info=True)


# ---------------------------------------------------------------- the envelope


def produced_refs(spec: SkillSpec | None, result: SkillResult) -> list[dict[str, Any]]:
    """The typed refs this result carries: declared ones found in ``value``, plus any the skill added."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    if spec is not None and spec.produces and isinstance(result.value, Mapping):
        for ref in spec.produces:
            value = result.value.get(ref.key)
            if isinstance(value, _SCALARS) and value not in ("", None):
                out.append({"kind": ref.kind, "value": value, "how_to_use": ref.how_to_use})
                seen.add(ref.kind)
    for item in result.produced or ():
        if isinstance(item, Mapping) and item.get("kind") and item.get("kind") not in seen:
            value = item.get("value")
            if isinstance(value, _SCALARS):
                out.append(
                    {
                        "kind": str(item["kind"]),
                        "value": value,
                        "how_to_use": str(item.get("how_to_use") or ""),
                    }
                )
                seen.add(str(item["kind"]))
    return out[:MAX_PRODUCED]


def _applies(step: NextStep, ok: bool, kinds: set[str]) -> bool:
    if step.when == "always":
        return True
    if step.when == "ok":
        return ok
    if step.when == "error":
        return not ok
    return step.when.partition(":")[2] in kinds


def next_steps_for(
    registry: SkillRegistry,
    spec: SkillSpec | None,
    result: SkillResult,
    produced: list[dict[str, Any]],
    *,
    surface: str,
) -> list[dict[str, Any]]:
    """Declared follow-ups that apply to this result and exist on this surface, plus the skill's own."""
    from axiom.infra.capability_projection import capability_to_surface_name, exposed_on

    candidates: list[NextStep] = list(spec.next_steps or ()) if spec is not None else []
    for item in result.next or ():
        step = coerce_next(item)
        if step is not None:
            candidates.append(step)

    kinds = {p["kind"] for p in produced}
    values = {p["kind"]: p["value"] for p in produced}
    exposure = _EXPOSURE_FOR_SURFACE.get(surface)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for step in candidates:
        if step.target in seen or not _applies(step, result.ok, kinds):
            continue
        entry: dict[str, Any] = {"when": step.when, "why": step.why}
        if step.is_skill:
            if not registry.has(step.target):
                continue
            target_spec = registry.spec(step.target)
            if exposure and target_spec is not None and not exposed_on(target_spec, exposure):
                continue
            if exposure and target_spec is None and exposure != "cli":
                continue
            entry = {"skill": step.target, **entry}
            if surface == "mcp":
                entry["tool"] = "axiom_" + capability_to_surface_name(step.target)
            if target_spec is not None and target_spec.effective_risk:
                entry["risk"] = target_spec.effective_risk
                if target_spec.effective_risk == "destructive":
                    entry["needs_approval"] = True
        else:
            if surface == "mcp" and step.target.lstrip().startswith("/"):
                continue  # a slash command is not reachable from a protocol client
            entry = {"command": step.target, **entry}
        fed = {name: values[kind] for name, kind in dict(step.feeds).items() if kind in values}
        if fed:
            entry["with"] = fed
            if surface == "mcp" and "tool" in entry:
                # The same pre-fill as an OpenAPI Link parameter expression: an
                # RFC 6901 pointer into this result's structured content.
                keys = {r.kind: r.key for r in ((spec.produces if spec is not None else None) or ())}
                entry["operationId"] = entry["tool"]
                entry["parameters"] = {
                    name: f"$response.body#/value/{keys.get(kind, kind)}"
                    for name, kind in dict(step.feeds).items()
                    if kind in values
                }
        seen.add(step.target)
        out.append(entry)
        if len(out) >= MAX_NEXT:
            break
    return out


def finish(
    registry: SkillRegistry,
    capability: str,
    result: SkillResult,
    prepared: Prepared,
    *,
    surface: str,
) -> SkillResult:
    """Decorate a finished result with ``produced`` / ``next`` / ``chain``; settle a claimed plan."""
    if prepared.claimed is not None and prepared.gate is not None:
        try:
            action = prepared.claimed
            if result.ok:
                action.complete({"value_keys": sorted(result.value) if isinstance(result.value, Mapping) else []})
            else:
                action.fail("; ".join(result.errors) or "skill reported failure")
            prepared.gate.record(action)
        except Exception:  # noqa: BLE001 - settling the record never changes the result
            log.warning("could not settle plan for %s", capability, exc_info=True)

    spec = registry.spec(capability)
    try:
        produced = produced_refs(spec, result)
        nexts = next_steps_for(registry, spec, result, produced, surface=surface)
    except Exception:  # noqa: BLE001 - advice never fails a call
        log.debug("chaining envelope not built for %s", capability, exc_info=True)
        produced, nexts = [], []
    result.produced = produced
    result.next = nexts
    if prepared.chain:
        result.chain = {k: v for k, v in prepared.chain.items() if v}
    return result


def envelope_payload(result: SkillResult) -> dict[str, Any] | None:
    """The ``chain`` key a transport adds to its payload, or ``None`` when empty."""
    return result.chain_envelope()


__all__ = [
    "MAX_NEXT",
    "MAX_PRODUCED",
    "Prepared",
    "UNGATED_SURFACES",
    "envelope_payload",
    "finish",
    "next_steps_for",
    "normalise_chain",
    "plan_digest",
    "prepare",
    "produced_refs",
    "split_chain_arg",
    "unmet_preconditions",
]
