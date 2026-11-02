# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.lane_{add,edit,remove}`` — the lane verbs.

A lane is a grouping with an ``id``, a ``name``, an optional ``color``, and an
optional ``lead`` principal the proxy-assignee rule reads (ADR-166). Lane
*definition* add/remove rides the snapshot diff (``lane_added`` / ``lane_removed``
on subject_kind ``lane``); a lead change is a first-class owner transition
(``lane_owner_changed``, old→new, provenance in ``by``) so the lane's ownership
over time is reconstructable even after a past lead leaves the roster; a
name/color change logs ``lane_edited``.

``lane remove`` refuses to orphan work: a lane that still owns schedule items
is removed only with ``--reassign-to`` naming another lane.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..model import ProgramValidationError
from . import _changelog as cl
from ._mutate import (
    ABSENT,
    BAD_REQUEST,
    CONFLICT,
    FORBIDDEN,
    authorize,
    commit,
    load_for_edit,
    mutable_copy,
    refuse,
    valid_principal,
)


def _lane_change(kind: str, lane_id: str, *, field: str | None = None, old: Any = None, new: Any = None) -> dict[str, Any]:
    return {
        "kind": kind,
        "subject_kind": cl.SUBJECT_LANE,
        "subject": lane_id,
        "field": field,
        "old": old,
        "new": new,
    }


def _gate(data, ctx) -> SkillResult | None:
    msg = authorize(data, ctx)
    return refuse(FORBIDDEN, msg) if msg is not None else None


# ---- add ------------------------------------------------------------------


def add(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    data, path, bad = load_for_edit(params, ctx)
    if bad is not None:
        return bad
    assert data is not None and path is not None
    denied = _gate(data, ctx)
    if denied is not None:
        return denied

    lane_id = params.get("id")
    if not isinstance(lane_id, str) or not lane_id.strip():
        return refuse(BAD_REQUEST, "lane id is required (--id)")
    if lane_id in data.lane_ids():
        return refuse(CONFLICT, f"lane {lane_id!r} already exists")

    lead = params.get("lead")
    if lead is not None and not valid_principal(lead):
        return refuse(BAD_REQUEST, f"--lead {lead!r} is not a principal of the form @name or @name:context")

    lane: dict[str, Any] = {"id": lane_id}
    if params.get("name"):
        lane["name"] = str(params["name"])
    if lead is not None:
        lane["lead"] = lead
    if params.get("color"):
        lane["color"] = str(params["color"])

    new_data = mutable_copy(data)
    new_data.raw.setdefault("lanes", []).append(lane)

    # lane_added is produced by the snapshot diff; nothing extra to log for the
    # definition. The initial lead is recovered by ownership history from the
    # lane_added timestamp plus the first lane_owner_changed's `old`.
    try:
        recorded = commit(ctx, path, data, new_data, [])
    except ProgramValidationError as exc:
        return refuse(BAD_REQUEST, "; ".join(exc.errors))

    return SkillResult(
        ok=True,
        value={"lane": lane, "changes": recorded},
        actions_taken=[f"added lane {lane_id}"],
    )


# ---- edit -----------------------------------------------------------------


def edit(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    data, path, bad = load_for_edit(params, ctx)
    if bad is not None:
        return bad
    assert data is not None and path is not None
    denied = _gate(data, ctx)
    if denied is not None:
        return denied

    lane_id = params.get("id")
    existing = data.lane(lane_id) if isinstance(lane_id, str) else None
    if existing is None:
        return refuse(ABSENT, f"no lane {lane_id!r} in the program")

    new_lead = params.get("lead")
    if new_lead is not None and new_lead != "" and not valid_principal(new_lead):
        return refuse(BAD_REQUEST, f"--lead {new_lead!r} is not a principal of the form @name or @name:context")

    new_data = mutable_copy(data)
    lane = new_data.lane(lane_id)
    assert lane is not None
    extra: list[dict[str, Any]] = []

    # name / color — lane_edited
    edited_fields: dict[str, Any] = {}
    for field in ("name", "color"):
        if params.get(field) is not None:
            old_v = lane.get(field)
            new_v = str(params[field])
            if old_v != new_v:
                lane[field] = new_v
                edited_fields[field] = {"old": old_v, "new": new_v}
    if edited_fields:
        extra.append(_lane_change("lane_edited", lane_id, new=edited_fields))

    # lead — lane_owner_changed (a first-class ownership transition)
    if new_lead is not None:
        old_lead = lane.get("lead")
        resolved_new = new_lead or None  # --lead "" clears the lead
        if old_lead != resolved_new:
            if resolved_new is None:
                lane.pop("lead", None)
            else:
                lane["lead"] = resolved_new
            extra.append(
                _lane_change("lane_owner_changed", lane_id, field="lead", old=old_lead, new=resolved_new)
            )

    if not extra:
        return refuse(BAD_REQUEST, "nothing to edit: pass --name, --color, or --lead")

    try:
        recorded = commit(ctx, path, data, new_data, extra)
    except ProgramValidationError as exc:
        return refuse(BAD_REQUEST, "; ".join(exc.errors))

    return SkillResult(
        ok=True,
        value={"lane": lane, "changes": recorded},
        actions_taken=[f"edited lane {lane_id}"],
    )


# ---- remove ---------------------------------------------------------------


def remove(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    data, path, bad = load_for_edit(params, ctx)
    if bad is not None:
        return bad
    assert data is not None and path is not None
    denied = _gate(data, ctx)
    if denied is not None:
        return denied

    lane_id = params.get("id")
    if not isinstance(lane_id, str) or data.lane(lane_id) is None:
        return refuse(ABSENT, f"no lane {lane_id!r} in the program")

    owned_items = data.items_for_lane(lane_id)
    reassign_to = params.get("reassign_to")
    if owned_items and not reassign_to:
        ids = ", ".join(i.get("id", "?") for i in owned_items)
        return refuse(
            CONFLICT,
            f"lane {lane_id!r} still owns item(s) {ids}; pass --reassign-to <lane-id> "
            "to move them, or remove/relane those items first",
        )
    if owned_items:
        if reassign_to == lane_id or data.lane(reassign_to) is None:
            return refuse(BAD_REQUEST, f"--reassign-to must name a different, existing lane (got {reassign_to!r})")

    new_data = mutable_copy(data)
    if owned_items:
        for entry in new_data.schedule:
            if entry.get("lane") == lane_id:
                entry["lane"] = reassign_to  # item lane_removed+lane_added via the diff
    # Strip the lane id from every member's lane list so the file stays valid.
    for person in new_data.people:
        person_lanes = person.get("lanes")
        if isinstance(person_lanes, list) and lane_id in person_lanes:
            person["lanes"] = [x for x in person_lanes if x != lane_id]
    new_data.raw["lanes"] = [ln for ln in new_data.lanes if ln.get("id") != lane_id]

    try:
        recorded = commit(ctx, path, data, new_data, [])  # lane_removed produced by the diff
    except ProgramValidationError as exc:
        return refuse(CONFLICT, "; ".join(exc.errors))

    return SkillResult(
        ok=True,
        value={"removed": lane_id, "reassigned_to": reassign_to if owned_items else None, "changes": recorded},
        actions_taken=[f"removed lane {lane_id}" + (f"; moved {len(owned_items)} item(s) to {reassign_to}" if owned_items else "")],
    )
