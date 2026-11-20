# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``notifications.recipient_set`` skill — write a RecipientProfile.

Per ADR-056 the CLI verb is a thin wrapper over this function.
"""

from __future__ import annotations

from typing import Any

from axiom.extensions.builtins.notifications.preferences import (
    RecipientProfile,
    default_store,
    parse_channel_spec,
)
from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    recipient = params.get("recipient")
    spec = params.get("channels")
    if not recipient:
        return SkillResult(ok=False, errors=["missing required param: recipient"])
    if not spec:
        return SkillResult(ok=False, errors=["missing required param: channels"])

    try:
        channels = parse_channel_spec(spec)
        profile = RecipientProfile(recipient=recipient, channels=channels)
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    store = params.get("_store") or default_store()
    store.put(profile)

    return SkillResult(
        ok=True,
        value={
            "resource": "recipient_profile",
            "recipient": profile.recipient,
            "channels": [
                {
                    "channel": c.channel,
                    "address": c.address,
                    "min_priority": c.min_priority.value,
                }
                for c in profile.channels
            ],
        },
        actions_taken=[
            f"recipient_set {profile.recipient} "
            f"({len(profile.channels)} channel(s))"
        ],
    )


__all__ = ["run"]
