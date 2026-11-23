# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What Needs you reads (ADR-253): one ask per open feedback group, for its owner only (LOOP-B5).

One item per cause, never one per report: the title counts the reports and the people who
sent them, and the primary action is the rollback already chosen ("Stop this flag" or "Roll
back the slot"); "Fix forward instead" asks for a reason. ``notices`` names the notification
receipts sent for the ask, so Needs you shows the ask and not its notification beside it.
"""

from __future__ import annotations

from typing import Any

from . import service, store


def _item(g: dict[str, Any]) -> dict[str, Any]:
    params = {"group": g["id"]}
    reporters = g.get("reporters") or 0
    title = (f"{service._count(g['bad'], 'bad report')} from {service._count(reporters, 'reporter')} "
             f"about {service.cause_label(g)}")
    return {
        "id": f"feedback:{g['id']}",
        "section": "due",
        "kind": "feedback",
        "title": title,
        "source": "feedback",
        "due": None,
        "priority": "high" if g.get("severe") else "normal",
        "href": service.href(g),
        "action": {"label": service.LABELS[g["rollback"]], "skill": "feedback.decide",
                   "params": {**params, "choice": g["rollback"]}},
        "choices": [{"label": service.LABELS[service.FIX_FORWARD], "skill": "feedback.decide",
                     "params": {**params, "choice": service.FIX_FORWARD}, "needs": ["reason"]}],
        "notices": [n.get("receipt") for n in g.get("notices") or [] if n.get("receipt")],
        "created_at": g.get("first_at"),
    }


def for_person(person: str) -> list[dict[str, Any]]:
    """The open feedback asks this person answers for, most recent first."""
    if not person:
        return []
    return [_item(g) for g in store.groups(status=("open",)) if service.holds(person, g["owner"])]


__all__ = ["for_person"]
