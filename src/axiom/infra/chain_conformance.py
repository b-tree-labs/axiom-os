# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Runtime conformance checks for the chaining contracts (AEOS 0.2 section 4.11.7).

Lint reads declarations; these functions run a real skill through the real
dispatch chokepoint and report where the *behaviour* breaks the contract. Each
returns a list of violations (empty means conforming) tagged with the rule id,
so an extension author can call them from the extension's own tests:

* AEOS141 ``check_requires_contract``: an unmet requirement has no effect, every
  unmet item carries a non-empty ``fix``, and following the fix clears it.
* AEOS140 ``check_plan_apply_contract``: a destructive skill from a gated surface
  does nothing on its first call, nothing on a forged hash, nothing under changed
  parameters, applies once under an approved plan, and nothing on replay.
* AEOS142 ``check_next_prefill``: a follow-up that declares ``feeds`` arrives with
  the argument already filled from the result, plus its pointer expression on MCP.
* AEOS150 ``check_audit_contract``: a supplied chain id is recorded with a step id
  in the action audit, the audit still verifies, and altering the chain id breaks
  verification (tamper-evident).

No mocks: callers pass a real registry and a real context in a temp state dir.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from axiom.infra import audit_trail
from axiom.infra.skill_dispatch import invoke_capability


def check_requires_contract(
    registry: Any,
    capability: str,
    unmet_params: dict[str, Any],
    ctx: Any,
    *,
    effects: Callable[[], int],
    remedy: Callable[[dict[str, Any]], dict[str, Any]],
    surface: str = "mcp",
) -> list[str]:
    """AEOS141. ``remedy(params)`` returns the params after literally following the ``fix``."""
    out: list[str] = []
    before = effects()
    r = invoke_capability(registry, capability, dict(unmet_params), ctx, surface=surface)
    if r.ok:
        out.append(f"AEOS141 {capability}: ran although a requirement was unmet")
    if effects() != before:
        out.append(f"AEOS141 {capability}: an unmet requirement had an effect")
    if not r.unmet:
        out.append(f"AEOS141 {capability}: refused without a structured unmet list")
    for item in r.unmet or []:
        if not str(item.get("fix", "")).strip():
            out.append(f"AEOS141 {capability}: unmet {item.get('id')!r} has no fix")
    fixed = invoke_capability(
        registry, capability, remedy(dict(unmet_params)), ctx, surface=surface
    )
    if not fixed.ok:
        out.append(
            f"AEOS141 {capability}: following the fix did not clear the requirement: {fixed.unmet or fixed.errors}"
        )
    return out


def check_plan_apply_contract(
    registry: Any,
    capability: str,
    params: dict[str, Any],
    other_params: dict[str, Any],
    ctx: Any,
    *,
    approve: Callable[[str], None],
    effects: Callable[[], int],
    surface: str = "mcp",
) -> list[str]:
    """AEOS140. ``approve(held_action_id)`` is the person's yes, through the real approval queue."""
    out: list[str] = []
    base = effects()
    first = invoke_capability(registry, capability, dict(params), ctx, surface=surface)
    if first.ok or effects() != base:
        out.append(f"AEOS140 {capability}: ran on the first call without an approved plan")
        return out
    value = first.value if isinstance(first.value, dict) else {}
    plan_hash, held = value.get("plan_hash"), value.get("held_action_id")
    if not plan_hash or not held:
        out.append(
            f"AEOS140 {capability}: the first call did not return a plan_hash and a held action"
        )
        return out
    forged = invoke_capability(
        registry, capability, {**params, "plan_hash": plan_hash}, ctx, surface=surface
    )
    if forged.ok or effects() != base:
        out.append(f"AEOS140 {capability}: a hash with no approval behind it applied")
    approve(held)
    swapped = invoke_capability(
        registry, capability, {**other_params, "plan_hash": plan_hash}, ctx, surface=surface
    )
    if swapped.ok or effects() != base:
        out.append(f"AEOS140 {capability}: an approved hash applied to different parameters")
    applied = invoke_capability(
        registry, capability, {**params, "plan_hash": plan_hash}, ctx, surface=surface
    )
    if not applied.ok or effects() != base + 1:
        out.append(f"AEOS140 {capability}: an approved plan did not apply exactly once")
    replay = invoke_capability(
        registry, capability, {**params, "plan_hash": plan_hash}, ctx, surface=surface
    )
    if replay.ok or effects() != base + 1:
        out.append(f"AEOS140 {capability}: an approval was usable twice")
    return out


def check_next_prefill(
    registry: Any, capability: str, params: dict[str, Any], ctx: Any, *, surface: str = "mcp"
) -> list[str]:
    """AEOS142. Every declared follow-up with ``feeds`` that applies arrives pre-filled."""
    out: list[str] = []
    spec = registry.spec(capability)
    r = invoke_capability(registry, capability, dict(params), ctx, surface=surface)
    if not r.ok:
        return [
            f"AEOS142 {capability}: the call failed ({r.errors or r.unmet}); cannot check follow-ups"
        ]
    produced = {p["kind"]: p["value"] for p in r.produced or []}
    nxt = {n.get("skill"): n for n in r.next or []}
    for step in (spec.next_steps if spec else None) or ():
        if not step.feeds or not step.is_skill or step.target not in nxt:
            continue
        entry = nxt[step.target]
        for arg, kind in step.feeds.items():
            if kind in produced and (entry.get("with") or {}).get(arg) != produced[kind]:
                out.append(
                    f"AEOS142 {capability}: follow-up {step.target} did not carry {arg} filled from {kind}"
                )
            if surface == "mcp" and kind in produced and arg not in (entry.get("parameters") or {}):
                out.append(
                    f"AEOS142 {capability}: follow-up {step.target} has no pointer expression for {arg}"
                )
    return out


def check_audit_contract(
    registry: Any, capability: str, params: dict[str, Any], ctx: Any, *, surface: str = "mcp"
) -> list[str]:
    """AEOS150. The chain id is recorded, the audit verifies, and tampering with it is detected."""
    out: list[str] = []
    path = audit_trail.audit_path(ctx.state_dir)
    invoke_capability(
        registry, capability, dict(params), ctx, surface=surface, chain={"chain_id": "conf-chain"}
    )
    recs = audit_trail.read_chain(path)
    last = recs[-1] if recs else {}
    if last.get("chain_id") != "conf-chain" or not last.get("step_id"):
        out.append(
            f"AEOS150 {capability}: the chain id and a step id were not recorded in the audit"
        )
    if not audit_trail.verify_chain(path):
        out.append(f"AEOS150 {capability}: the audit does not verify after a chained call")
    original = path.read_text()
    path.write_text(original.replace("conf-chain", "conf-other"))
    try:
        if audit_trail.verify_chain(path):
            out.append(f"AEOS150 {capability}: altering the chain id was not detected")
    finally:
        path.write_text(original)
    return out


__all__ = [
    "check_audit_contract",
    "check_next_prefill",
    "check_plan_apply_contract",
    "check_requires_contract",
]
