# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``schedule.items_list`` / ``schedule.items_show``: the agenda read (MVP-24).

Read-only on every surface (CLI, MCP, chat, the web): the providers in
``schedule.items`` only select, and these skills are registered non-mutating,
so a read leaves no audit row either.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def list_items(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from .. import items

    p = dict(params or {})
    if p.get("days") not in (None, ""):
        try:
            p["days"] = int(p["days"])
        except (TypeError, ValueError):
            return SkillResult(ok=False, errors=["days is a whole number of days"])
        if p["days"] < 1:
            return SkillResult(ok=False, errors=["days is at least 1"])
    if p.get("source") and p["source"] not in {pr.name for pr in items.providers()}:
        names = ", ".join(sorted(pr.name for pr in items.providers()))
        return SkillResult(ok=False, errors=[f"no agenda source {p['source']!r}; sources: {names}"])
    try:
        value = items.items_list(p)
    except ValueError as exc:
        return SkillResult(ok=False, errors=[f"bad window: {exc}"])
    return SkillResult(ok=True, value=value)


def show_item(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from .. import items

    item_id = str((params or {}).get("id") or "").strip()
    if not item_id:
        return SkillResult(ok=False, errors=["name the item: id is required"])
    found = items.items_show(item_id)
    if found is None:
        return SkillResult(ok=False, errors=[f"no agenda item {item_id!r}"])
    tz = items.site_timezone()
    return SkillResult(ok=True, value={"tz": tz, "tz_label": items.tz_label(tz), "item": found})


SPECS = (
    (
        "schedule.items_list",
        list_items,
        "The agenda: scheduler runs, attest obligations of a day or longer, and vault "
        "expiries in a window, in site time with the zone stated. Read-only.",
        {
            "from": "window start: a date (YYYY-MM-DD, site time) or an ISO instant; default today",
            "to": "window end: a date (inclusive) or an ISO instant",
            "days": "window length in days when 'to' is not given (default 7)",
            "source": "only this source: schedule, attest or vault",
            "person": "only items owned by or addressed to this principal",
        },
    ),
    (
        "schedule.items_show",
        show_item,
        "One agenda item by its id, in site time. Read-only.",
        {"id": "the item id, as items_list returns it (required)"},
    ),
)


__all__ = ["SPECS", "list_items", "show_item"]
