# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.renewable`` — can this credential be rotated without a human?

The *mechanism* half of KEEP's expiry-rotation policy (the split is the same one
``vault/skills/sweep.py`` documents, per ``secrets/docs/decisions/adr-001``):
``secrets`` owns operational credentials and therefore owns the facts about
whether one is mechanically rotatable. ``vault`` (KEEP) owns the policy that
decides what to *do* with those facts.

Two independent gates, and both must pass:

1. **The provider can rotate unattended.** ``RotationProvider`` implementations
   already declare this as ``interactive``: ``GitLabPatProvider`` sets
   ``interactive = False`` and ships a real ``probe()``; ``GuidedRotationProvider``
   sets ``interactive = True`` and raises headless because a human has to mint the
   replacement at a console. This is read, not re-derived.

2. **The operator has opted this credential in** via ``auto_rotate`` in its
   metadata. This gate is deliberately a human assertion rather than an
   inference, because the failure it guards against is a human one: rotating a
   credential whose consumers hold a *copied* value silently breaks them. That
   is not hypothetical — a read-only database credential was rotated, the new
   value propagated to three of the five env files that held a copy,
   and the affected service kept reporting healthy while every
   query behind it failed overnight. A credential is safe to auto-rotate only
   when its consumers resolve the value dynamically (a credential helper, a
   vault reference) rather than holding a copy, and only a person can attest to
   that.

Gate 1 without gate 2 is the dangerous combination: mechanically rotatable, and
nobody has checked who is holding a copy.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..foreign.rotation_providers import rotation_provider_kinds
from ..foreign.store import ForeignCredentialStore

# Provider kinds that can mint a replacement with no human present. Derived from
# the provider classes' own `interactive` flag rather than a second hardcoded
# list, so a new unattended provider participates automatically.
_UNATTENDED_KINDS_CACHE: set[str] | None = None


def unattended_provider_kinds() -> set[str]:
    """Provider kinds whose ``rotate()`` needs no human at the keyboard."""
    global _UNATTENDED_KINDS_CACHE
    if _UNATTENDED_KINDS_CACHE is not None:
        return _UNATTENDED_KINDS_CACHE
    from ..foreign import rotation_providers as rp

    kinds: set[str] = set()
    for kind in rotation_provider_kinds():
        cls = getattr(rp, "_KINDS", {}).get(kind)
        if cls is not None and getattr(cls, "interactive", True) is False:
            kinds.add(kind)
    _UNATTENDED_KINDS_CACHE = kinds
    return kinds


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "on", "1", "yes")


def classify(meta: dict) -> dict:
    """Return renewability facts for one credential's metadata.

    Reports both gates separately so the caller can explain *which* one blocked
    a rotation — "no unattended provider" and "not opted in" need different
    human actions, and collapsing them into one boolean loses that.
    """
    name = meta.get("name", "<unnamed>")
    provider = str(meta.get("provider") or "").strip()
    unattended = provider in unattended_provider_kinds()
    opted_in = _truthy(meta.get("auto_rotate", False))

    if unattended and opted_in:
        reason = "provider rotates unattended and the operator opted in"
    elif not unattended and not opted_in:
        reason = (
            f"provider '{provider or 'unset'}' needs a human to mint the "
            f"replacement, and the credential is not opted in"
        )
    elif not unattended:
        reason = (
            f"provider '{provider or 'unset'}' needs a human to mint the "
            f"replacement at the issuer console"
        )
    else:
        reason = (
            "not opted in — set auto_rotate once you have confirmed every "
            "consumer resolves this value dynamically rather than holding a copy"
        )

    return {
        "name": name,
        "provider": provider,
        "provider_unattended": unattended,
        "operator_opted_in": opted_in,
        "auto_renewable": unattended and opted_in,
        "reason": reason,
    }


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    store = params.get("_store") or ForeignCredentialStore(ctx.state_dir)
    only = params.get("name")
    try:
        rows = [classify(m) for m in store.list() if not only or m.get("name") == only]
    except Exception as exc:  # noqa: BLE001
        return SkillResult(ok=False, errors=[f"renewability check failed: {exc}"])

    return SkillResult(
        ok=True,
        value={
            "credentials": rows,
            "auto_renewable": sum(1 for r in rows if r["auto_renewable"]),
            "total": len(rows),
        },
        actions_taken=[f"classified {len(rows)} credential(s)"],
    )


__all__ = ["classify", "run", "unattended_provider_kinds"]
