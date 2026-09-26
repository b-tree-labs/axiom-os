# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``principal.verify`` — the comms test run.

Sends a real message on every declared endpoint and records what actually
happened. Delivery through HERALD is resolved lazily so this module stays
testable without a notifications stack.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..models import ContactEndpoint
from ..store import load, save
from ..verify import DEFAULT_MESSAGE, verify_endpoints, verify_endpoints_machine


def _herald_sender():
    """Return a sender backed by HERALD, or None when notifications are absent.

    A receipt id is the only accepted evidence of delivery — an adapter that
    returns nothing is treated as unproven, never as sent.
    """
    try:
        from axiom.extensions.builtins.notifications import public_api
    except Exception:  # noqa: BLE001
        return None

    def _send(endpoint: ContactEndpoint, message: str) -> str | None:
        receipt = public_api.send(
            recipient=endpoint.address,
            summary=message,
            priority="low",
            dedup_key=f"principal-verify:{endpoint.kind}",
        )
        return getattr(receipt, "receipt_id", None) or (
            receipt.get("receipt_id") if isinstance(receipt, dict) else None
        )

    return _send


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    config_dir = params.get("config_dir")
    principal = load(config_dir=config_dir)
    if principal is None:
        return SkillResult(ok=False, errors=["no principal configured; run `axi principal setup`"])

    # A non-human principal is proven by driving both ends rather than by waiting
    # for a reply nobody will send (spec §3.2). This makes `verify` a re-runnable
    # liveness probe for service principals instead of a one-time ceremony.
    if not principal.requires_interview:
        roundtrip = params.get("roundtrip")
        if roundtrip is None:
            return SkillResult(
                ok=False,
                errors=[
                    f"{principal.handle} is a {principal.kind.value} principal, which is "
                    "proven by a machine round trip; no probe was supplied, so nothing "
                    "is claimed about its endpoints"
                ],
            )
        outcome = verify_endpoints_machine(principal.endpoints, roundtrip=roundtrip)
        return _finish(principal, outcome, config_dir=config_dir, mode="round trip")

    send = params.get("send") or _herald_sender()
    if send is None:
        return SkillResult(
            ok=False,
            errors=[
                "notifications extension unavailable — cannot prove delivery, so nothing is claimed"
            ],
        )

    outcome = verify_endpoints(
        principal.endpoints, send=send, message=params.get("message") or DEFAULT_MESSAGE
    )
    return _finish(principal, outcome, config_dir=config_dir, mode="comms test")


def _finish(principal, outcome, *, config_dir, mode: str) -> SkillResult:
    principal.refresh_status()
    save(principal, config_dir=config_dir)

    actions = [f"{mode.capitalize()}: {outcome.summary()}"]
    if outcome.degraded:
        actions.append(
            "Unproven endpoints are recorded but will NOT be selected by routing — "
            "a configured channel is not a delivering one."
        )
    return SkillResult(
        ok=outcome.any_verified,
        value={
            "verified": outcome.verified,
            "degraded": outcome.degraded,
            "fully_verified": outcome.fully_verified,
            "status": principal.status.value,
        },
        errors=[] if outcome.any_verified else ["no endpoint completed a round trip"],
        actions_taken=actions,
    )
