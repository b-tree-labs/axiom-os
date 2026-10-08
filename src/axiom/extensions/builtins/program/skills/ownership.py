# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.ownership`` — current owner plus the ownership timeline.

``data.json`` carries the CURRENT owner only. Ownership over time is history,
and history lives in the append-only change log. This read reports both: the
current owner from the data file, and the timeline reconstructed from the
``owner_changed`` (item) / ``lane_owner_changed`` (lane) transitions — each a
span of ``principal`` + ``from``/``to`` timestamps, with ``set_by`` naming who
made the change.

Because the timeline comes from the immutable log and not from ``people[]``, a
principal who held ownership in the past is reported accurately even after they
are removed from the roster — the owner at that time is a fact that a later
removal cannot rewrite. Read-only: this skill never writes.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..model import ProgramError, load_program
from . import _changelog as cl
from ._source import ABSENT, BAD_REQUEST, resolve_data_path

SCOPES = ("item", "lane")

#: scope → (the transition kind, the creation kind that dates the first span).
_TIMELINE = {
    "item": ("owner_changed", "item_added"),
    "lane": ("lane_owner_changed", "lane_added"),
}


def _refuse(kind: str, message: str) -> SkillResult:
    return SkillResult(ok=False, value={"refused": kind}, errors=[message])


def _timeline(
    changelog: list[dict[str, Any]], subject: str, transition_kind: str, creation_kind: str, current: Any
) -> list[dict[str, Any]]:
    transitions = sorted(
        (e for e in changelog if e.get("subject") == subject and e.get("kind") == transition_kind),
        key=lambda e: e.get("seq", 0),
    )
    creation = next(
        (e for e in changelog if e.get("subject") == subject and e.get("kind") == creation_kind), None
    )
    creation_ts = creation.get("ts") if creation else None
    creation_by = creation.get("by") if creation else None

    spans: list[dict[str, Any]] = []
    if transitions:
        first = transitions[0]
        spans.append(
            {"principal": first.get("old"), "from": creation_ts, "to": first.get("ts"), "set_by": creation_by}
        )
        for i, t in enumerate(transitions):
            nxt = transitions[i + 1].get("ts") if i + 1 < len(transitions) else None
            spans.append({"principal": t.get("new"), "from": t.get("ts"), "to": nxt, "set_by": t.get("by")})
    else:
        spans.append({"principal": current, "from": creation_ts, "to": None, "set_by": creation_by})
    return spans


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    scope = params.get("scope")
    if scope not in SCOPES:
        return _refuse(BAD_REQUEST, f"scope must be one of {', '.join(SCOPES)} (got {scope!r})")
    key = params.get("key")
    if not key:
        return _refuse(BAD_REQUEST, f"scope {scope!r} requires a --key (the item or lane id)")

    data_path, refusal = resolve_data_path(params, ctx)
    if refusal is not None:
        return _refuse(BAD_REQUEST, refusal)
    try:
        data = load_program(data_path, require_listed_owners=False)
    except ProgramError:
        data = None

    if scope == "item":
        entry = data.item(key) if data is not None else None
        if entry is None:
            return _refuse(ABSENT, f"no item {key!r} in the schedule")
        current = entry.get("owner")
    else:
        lane = data.lane(key) if data is not None else None
        if lane is None:
            return _refuse(ABSENT, f"no lane {key!r} in the program")
        current = lane.get("lead")

    transition_kind, creation_kind = _TIMELINE[scope]
    changelog = cl.read_changelog(cl.changelog_path(ctx))
    history = _timeline(changelog, key, transition_kind, creation_kind, current)

    return SkillResult(
        ok=True,
        value={
            "scope": scope,
            "key": key,
            "current": current,
            "history": history,
            "count": len(history),
        },
    )
