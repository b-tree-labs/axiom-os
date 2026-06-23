# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``notifications.recipient_show`` skill — read one RecipientProfile."""

from __future__ import annotations

from typing import Any

from axiom.extensions.builtins.notifications.preferences import default_store
from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    recipient = params.get("recipient")
    if not recipient:
        return SkillResult(ok=False, errors=["missing required param: recipient"])

    store = params.get("_store") or default_store()
    profile = store.get(recipient)
    if profile is None:
        return SkillResult(
            ok=False,
            errors=[f"no recipient profile registered for {recipient!r}"],
            value={"resource": "recipient_profile", "recipient": recipient},
        )

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
    )


__all__ = ["run"]
