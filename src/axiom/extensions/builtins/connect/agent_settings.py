# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Per-principal agent settings, applied by an authorized human (incl. via chat).

Humans tune their agent — first example: a nickname, so "MECH-A94401's Axi"
becomes "Rivet's Axi". Settings are keyed by the agent principal; writes are
authorized through the ownership model (ADR-026). The settable keys are a
**whitelist**, each declaring the right it needs and whether it's *sensitive*
(sensitive keys require step-up re-verification — the "wiggle room" guardrail on
a channel-bound identity that otherwise inherits the owner's full authority).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from axiom.memory.ownership import Ownership, Right, can_exercise


class StepUpRequired(PermissionError):
    """A sensitive setting needs step-up re-verification beyond the channel login."""


class NotAuthorized(PermissionError):
    """The requester lacks the right to change this setting."""


@dataclass(frozen=True)
class SettingSpec:
    right: Right            # ownership right required to change it
    sensitive: bool = False  # requires step-up re-verification
    doc: str = ""


# The whitelist of agentically-settable keys. New keys are added here (data),
# not by widening code paths.
WHITELIST: dict[str, SettingSpec] = {
    "owner.nickname": SettingSpec(Right.GOALS, False,
                                  "Possessive owner token, e.g. 'Rivet' → 'Rivet's Axi'"),
    "agent.prose_style": SettingSpec(Right.GOALS, False, "How the agent should phrase replies"),
    "comms.quiet_hours": SettingSpec(Right.GOALS, False, "When not to ping (off-hours)"),
    # Sensitive — inherited authority is not enough; needs step-up:
    "secrets.rotate": SettingSpec(Right.CONTROL, True, "Rotate the connector's stored secret"),
    "identity.rebind": SettingSpec(Right.CONTROL, True, "Re-bind which human this agent serves"),
}


def _path(principal: str) -> Path:
    from axiom.infra.paths import get_user_state_dir

    slug = principal.lstrip("@").replace(":", "-").replace("/", "-")
    return get_user_state_dir() / "agent-settings" / f"{slug}.json"


def get_setting(principal: str, key: str, *, path: Path | None = None) -> Any:
    p = path or _path(principal)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text()).get(key)
    except (json.JSONDecodeError, OSError):
        return None


def set_setting(
    principal: str,
    key: str,
    value: Any,
    *,
    requester: str,
    ownership: Ownership,
    stepped_up: bool = False,
    at: str | None = None,
    path: Path | None = None,
) -> dict:
    """Apply a whitelisted setting on ``principal`` if ``requester`` is authorized.

    Raises ``KeyError`` (unknown key), ``NotAuthorized`` (missing right), or
    ``StepUpRequired`` (sensitive key without step-up)."""
    spec = WHITELIST.get(key)
    if spec is None:
        raise KeyError(f"{key!r} is not a settable agent setting")
    from datetime import UTC, datetime

    when = at or datetime.now(UTC).isoformat()
    if not can_exercise(ownership, requester, spec.right, when):
        raise NotAuthorized(f"{requester} lacks {spec.right.value} to set {key}")
    if spec.sensitive and not stepped_up:
        raise StepUpRequired(f"{key} is sensitive — re-verify before changing it")

    p = path or _path(principal)
    data = json.loads(p.read_text()) if p.exists() else {}
    data[key] = value
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
    return {"principal": principal, "key": key, "value": value, "set_by": requester}


__all__ = ["WHITELIST", "SettingSpec", "StepUpRequired", "NotAuthorized",
           "get_setting", "set_setting"]
