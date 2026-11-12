# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.item_{add,edit,remove,reassign}`` — the schedule verbs.

A schedule item is work with an ``id``, a ``label``, a date (a single ``date``
or a ``start``/``end`` span), an optional ``lane``, an optional ``owner``
principal (which must be a member), a proposed-or-committed ``status``, a
``pct``, and an optional tracker ``issue`` reference.

The tracked fields (owner, status, pct, issue, lane, dates) log through the
snapshot diff exactly as ``sync`` logs them; a label change logs ``item_edited``.
``item reassign`` changes the owner and, per the ADR-166 proxy-assignee rule,
recomputes ``item['assignment']`` when the new owner has no account on the
program's tracker system — **recorded, never posted to any tracker**.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..model import STATUS_VALUES, ProgramValidationError
from . import _changelog as cl
from ._mutate import (
    ABSENT,
    BAD_REQUEST,
    CONFLICT,
    FORBIDDEN,
    apply_owner,
    authorize,
    commit,
    load_for_edit,
    mutable_copy,
    refuse,
    valid_principal,
)


def _gate(data, ctx) -> SkillResult | None:
    msg = authorize(data, ctx)
    return refuse(FORBIDDEN, msg) if msg is not None else None


def _validate_owner(data, owner) -> SkillResult | None:
    if not valid_principal(owner):
        return refuse(BAD_REQUEST, f"--owner {owner!r} is not a principal of the form @name or @name:context")
    if data.person(owner) is None:
        return refuse(
            BAD_REQUEST,
            f"--owner {owner} is not a member of this program; an item's owner must be a "
            "principal the ownership map knows (add them with `program person add` first)",
        )
    return None


def _apply_scalar_fields(item: dict[str, Any], params: dict[str, Any]) -> SkillResult | None:
    """Apply label/status/pct/issue/lane/date fields present in ``params`` to
    ``item`` in place. Returns a refusal for a malformed value, else ``None``.
    Owner is handled separately (it drives the proxy rule)."""
    if params.get("label") is not None:
        item["label"] = str(params["label"])
    if params.get("status") is not None:
        status = str(params["status"])
        if status not in STATUS_VALUES:
            return refuse(BAD_REQUEST, f"--status must be one of {' | '.join(STATUS_VALUES)}")
        item["status"] = status
    if params.get("pct") is not None:
        try:
            item["pct"] = int(params["pct"])
        except (TypeError, ValueError):
            return refuse(BAD_REQUEST, f"--pct must be an integer 0..100, got {params['pct']!r}")
    if params.get("issue") is not None:
        item["issue"] = params["issue"]
    if params.get("lane") is not None:
        item["lane"] = str(params["lane"])
    for field in ("date", "start", "end"):
        if params.get(field) is not None:
            item[field] = str(params[field])
    return None


# ---- add ------------------------------------------------------------------


def add(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    data, path, bad = load_for_edit(params, ctx)
    if bad is not None:
        return bad
    assert data is not None and path is not None
    denied = _gate(data, ctx)
    if denied is not None:
        return denied

    item_id = params.get("id")
    if not isinstance(item_id, str) or not item_id.strip():
        return refuse(BAD_REQUEST, "item id is required (--id)")
    if data.item(item_id) is not None:
        return refuse(CONFLICT, f"item {item_id!r} already exists")
    if not params.get("label"):
        return refuse(BAD_REQUEST, "item label is required (--label)")

    lane = params.get("lane")
    if lane is not None and lane not in data.lane_ids():
        return refuse(BAD_REQUEST, f"unknown lane {lane!r}")

    owner = params.get("owner")
    if owner is not None:
        owner_bad = _validate_owner(data, owner)
        if owner_bad is not None:
            return owner_bad

    item: dict[str, Any] = {"id": item_id}
    scalar_bad = _apply_scalar_fields(item, params)
    if scalar_bad is not None:
        return scalar_bad

    new_data = mutable_copy(data)
    new_data.raw.setdefault("schedule", []).append(item)
    if owner is not None:
        apply_owner(new_data, item, owner)  # sets owner + any proxy assignment

    try:
        recorded = commit(ctx, path, data, new_data, [])  # item_added via the diff
    except ProgramValidationError as exc:
        return refuse(BAD_REQUEST, "; ".join(exc.errors))

    return SkillResult(
        ok=True,
        value={"item": item, "changes": recorded},
        actions_taken=[f"added item {item_id}"],
    )


# ---- edit -----------------------------------------------------------------


def edit(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Edit label/lane/dates/status/pct/issue. Owner changes go through
    ``item reassign`` so they log and recompute the proxy assignment."""
    data, path, bad = load_for_edit(params, ctx)
    if bad is not None:
        return bad
    assert data is not None and path is not None
    denied = _gate(data, ctx)
    if denied is not None:
        return denied

    item_id = params.get("id")
    existing = data.item(item_id) if isinstance(item_id, str) else None
    if existing is None:
        return refuse(ABSENT, f"no item {item_id!r} in the schedule")

    lane = params.get("lane")
    if lane is not None and lane not in data.lane_ids():
        return refuse(BAD_REQUEST, f"unknown lane {lane!r}")

    new_data = mutable_copy(data)
    item = new_data.item(item_id)
    assert item is not None
    old_label = item.get("label")

    scalar_bad = _apply_scalar_fields(item, params)
    if scalar_bad is not None:
        return scalar_bad

    extra: list[dict[str, Any]] = []
    if params.get("label") is not None and item.get("label") != old_label:
        extra.append(
            {
                "kind": "item_edited",
                "subject_kind": cl.SUBJECT_ITEM,
                "subject": item_id,
                "field": "label",
                "old": old_label,
                "new": item.get("label"),
            }
        )

    try:
        recorded = commit(ctx, path, data, new_data, extra)
    except ProgramValidationError as exc:
        return refuse(BAD_REQUEST, "; ".join(exc.errors))

    if not recorded:
        return refuse(BAD_REQUEST, "nothing changed")

    return SkillResult(
        ok=True,
        value={"item": item, "changes": recorded},
        actions_taken=[f"edited item {item_id}"],
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

    item_id = params.get("id")
    if not isinstance(item_id, str) or data.item(item_id) is None:
        return refuse(ABSENT, f"no item {item_id!r} in the schedule")

    new_data = mutable_copy(data)
    new_data.raw["schedule"] = [e for e in new_data.schedule if e.get("id") != item_id]

    try:
        recorded = commit(ctx, path, data, new_data, [])  # item_removed via the diff
    except ProgramValidationError as exc:
        return refuse(BAD_REQUEST, "; ".join(exc.errors))

    return SkillResult(
        ok=True,
        value={"removed": item_id, "changes": recorded},
        actions_taken=[f"removed item {item_id}"],
    )


# ---- reassign -------------------------------------------------------------


def reassign(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Change an item's owner, recomputing the proxy assignment (ADR-166)."""
    data, path, bad = load_for_edit(params, ctx)
    if bad is not None:
        return bad
    assert data is not None and path is not None
    denied = _gate(data, ctx)
    if denied is not None:
        return denied

    item_id = params.get("id")
    existing = data.item(item_id) if isinstance(item_id, str) else None
    if existing is None:
        return refuse(ABSENT, f"no item {item_id!r} in the schedule")

    new_owner = params.get("owner")
    owner_bad = _validate_owner(data, new_owner)
    if owner_bad is not None:
        return owner_bad
    if existing.get("owner") == new_owner:
        return refuse(BAD_REQUEST, f"item {item_id} is already owned by {new_owner}")

    new_data = mutable_copy(data)
    item = new_data.item(item_id)
    assert item is not None
    apply_owner(new_data, item, new_owner)  # owner_changed via the diff; assignment recomputed

    try:
        recorded = commit(ctx, path, data, new_data, [])
    except ProgramValidationError as exc:
        return refuse(BAD_REQUEST, "; ".join(exc.errors))

    return SkillResult(
        ok=True,
        value={"item": item, "assignment": item.get("assignment"), "changes": recorded},
        actions_taken=[f"reassigned item {item_id} to {new_owner}"],
    )
