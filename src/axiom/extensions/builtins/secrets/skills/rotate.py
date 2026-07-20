# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.rotate`` — rotate one secret now (ADR-095 / ADR-080).

`axi secrets rotate <ref> [--force] [--strategy K] [--value V] [--overlap S]`

Drives the rotation engine for a single ref: pick the strategy, mint/stage the
new credential, and — when the overlap window is zero (or `--retire-now`) —
revoke the superseded one inline. With a non-zero overlap the old credential
stays valid and its retirement is left to the schedule (PULSE) reconciler; the
skill reports the window so an operator isn't guessing.

`--force` is the leaked-key closer: rotate regardless of cadence, without
recreating keys by hand in a vendor console. This skill NEVER prints a secret
value — only the rotation outcome (versions, window, strategy).

Strategies usable from the CLI today: ``provider-native`` (the backend rotates
itself) and ``hitl`` (a human supplies the new value via ``--value`` or the
interactive prompt). Vendor-API strategies (``sendgrid`` …) need their admin
client wired from a configured credential ref — that config plumbing is a
follow-on; the skill returns a clear error rather than half-doing it.
"""

from __future__ import annotations

import time
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..providers.protocol import SecretRef
from ..providers.registry import SecretStoreRegistry
from ..rotation import NotDue, RotationEngine, RotationPolicy
from ..rotation.strategies import (
    HitlRotation,
    ProviderNativeRotation,
    RotationError,
)

_CLI_STRATEGIES = ("provider-native", "hitl")
_VENDOR_STRATEGIES = ("sendgrid",)  # need an admin-client ref (config follow-on)


def _store_for(scheme: str) -> Any:
    from .. import _default_config_for_scheme  # local import: avoid cycle

    provider_cls = SecretStoreRegistry.get(scheme)
    provider = provider_cls(_default_config_for_scheme(scheme))
    return provider.open()


def _build_strategy(kind: str, params: dict, ctx: SkillContext):
    if kind == "provider-native":
        return ProviderNativeRotation()
    if kind == "hitl":
        def _value_provider(ref: SecretRef) -> bytes:
            raw = params.get("value")
            if raw:
                return raw.encode("utf-8") if isinstance(raw, str) else raw
            if ctx.user_prompt is not None:
                entered = ctx.user_prompt(f"Paste the new credential for {ref}: ")
                return entered.encode("utf-8") if entered else b""
            return b""  # headless + no --value → HitlRotation raises RotationError

        return HitlRotation(
            value_provider=_value_provider,
            notifier=lambda msg: ctx.logger.warning(msg),
        )
    raise KeyError(kind)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    errors: list[str] = []

    raw_ref = params.get("ref")
    if not raw_ref:
        return SkillResult(ok=False, errors=["rotate needs a SecretRef (e.g. openbao://kv/x)"])
    try:
        ref = SecretRef.parse(raw_ref)
    except ValueError as exc:
        return SkillResult(ok=False, errors=[f"bad ref {raw_ref!r}: {exc}"])

    kind = params.get("strategy", "provider-native")
    if kind in _VENDOR_STRATEGIES:
        return SkillResult(
            ok=False,
            errors=[
                f"strategy {kind!r} is a vendor-API strategy; it needs its admin "
                "client wired from a configured credential ref (rotation config — "
                "follow-on). Usable from the CLI today: "
                f"{', '.join(_CLI_STRATEGIES)}."
            ],
        )
    try:
        strategy = _build_strategy(kind, params, ctx)
    except KeyError:
        return SkillResult(
            ok=False,
            errors=[f"unknown strategy {kind!r}; known: {', '.join(_CLI_STRATEGIES)}"],
        )

    overlap = int(params.get("overlap", 0))
    cadence = params.get("cadence")
    policy = RotationPolicy(
        cadence_seconds=int(cadence) if cadence is not None else None,
        overlap_seconds=overlap,
    )
    force = bool(params.get("force", False))

    engine = RotationEngine(
        resolver=lambda _ref: strategy,
        store_for=_store_for,
        clock=time.time,
    )

    try:
        outcome = engine.rotate(
            ref, policy=policy, force=force,
            last_rotated_at=params.get("last_rotated_at"),
        )
    except NotDue as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    except RotationError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    except (KeyError, PermissionError, RuntimeError) as exc:
        return SkillResult(ok=False, errors=[f"rotation failed: {exc}"])

    actions = [f"rotated {ref} via {outcome.strategy}"
               + (" (forced)" if outcome.forced else "")]
    pending = engine.pending_revocations()
    if pending:
        actions.append(
            f"previous credential valid until {outcome.old_valid_until}; "
            "retirement deferred to the schedule reconciler"
        )
    else:
        actions.append("previous credential retired inline (zero overlap)")

    value = {
        "ref": str(ref),
        "strategy": outcome.strategy,
        "forced": outcome.forced,
        "rotated_at": outcome.rotated_at,
        "new_version": outcome.new_version,
        "old_valid_until": outcome.old_valid_until,
        "revoke_at": outcome.revoke_at,
        "pending_revocations": pending,
    }
    return SkillResult(ok=True, value=value, actions_taken=actions, errors=errors)


__all__ = ["run"]
