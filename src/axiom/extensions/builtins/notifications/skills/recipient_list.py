# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``notifications.recipient_list`` skill — list all RecipientProfiles."""

from __future__ import annotations

from typing import Any

from axiom.extensions.builtins.notifications.preferences import default_store
from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    store = params.get("_store") or default_store()
    profiles = store.list()
    items = [
        {
            "recipient": p.recipient,
            "channels": [
                {
                    "channel": c.channel,
                    "address": c.address,
                    "min_priority": c.min_priority.value,
                }
                for c in p.channels
            ],
        }
        for p in profiles
    ]
    return SkillResult(
        ok=True,
        value={
            "resource": "recipient_profiles",
            "items": items,
            "count": len(items),
        },
    )


__all__ = ["run"]
