# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.person_{add,edit,remove,reassign}`` — the membership verbs.

A program member is a MEMBERSHIP, not an identity: a principal
(``@name:context``, the platform's one grammar) layered with a program lane
and role, plus an optional ``accounts`` map, over the principal primitive
(ADR-166, ADR-167). The program keeps no parallel people registry — the
handle is resolved through the directory seam when a provider is configured,
and accepted as given when none is (:func:`._mutate.resolve_member_identity`).

Each verb gates on the deputy/maintainer rule, mutates ``people[]`` losslessly,
and appends a ``person_*`` change-log entry naming who acted. None of them need
GitLab, GitHub, or any harness present.
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
    apply_owner,
    authorize,
    commit,
    load_for_edit,
    mutable_copy,
    parse_accounts,
    refuse,
    resolve_member_identity,
    valid_principal,
)


def _lanes_param(params: dict[str, Any]) -> list[str]:
    """``--lane`` is repeatable; normalise to a list of lane ids."""
    raw = params.get("lane")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    return [str(x) for x in raw]


def _person_change(kind: str, principal: str, *, old: Any = None, new: Any = None) -> dict[str, Any]:
    return {
        "kind": kind,
        "subject_kind": cl.SUBJECT_PERSON,
        "subject": principal,
        "field": None,
        "old": old,
        "new": new,
    }


def _forbidden(data, ctx) -> SkillResult | None:
    from ._mutate import FORBIDDEN

    msg = authorize(data, ctx)
    return refuse(FORBIDDEN, msg) if msg is not None else None


# ---- add ------------------------------------------------------------------


def add(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    data, path, bad = load_for_edit(params, ctx)
    if bad is not None:
        return bad
    assert data is not None and path is not None
    denied = _forbidden(data, ctx)
    if denied is not None:
        return denied

    principal = params.get("principal")
    if not valid_principal(principal):
        return refuse(BAD_REQUEST, f"{principal!r} is not a principal of the form @name or @name:context")
    if data.person(principal) is not None:
        return refuse(CONFLICT, f"{principal} is already a member of this program")

    lanes = _lanes_param(params)
    unknown = [lane for lane in lanes if lane not in data.lane_ids()]
    if unknown:
        return refuse(BAD_REQUEST, f"unknown lane(s): {', '.join(unknown)}")

    accounts, acc_err = parse_accounts(params.get("account"))
    if acc_err is not None:
        return refuse(BAD_REQUEST, acc_err)

    advisory = resolve_member_identity(principal)

    person: dict[str, Any] = {"principal": principal, "lanes": lanes}
    if params.get("name"):
        person["name"] = str(params["name"])
    if params.get("role"):
        person["role"] = str(params["role"])
    if params.get("drives"):
        person["drives"] = str(params["drives"])
    if accounts is not None:
        person["accounts"] = accounts

    new_data = mutable_copy(data)
    new_data.raw.setdefault("people", []).append(person)

    change = _person_change(
        "person_added", principal, new={"lanes": lanes, "role": person.get("role")}
    )
    try:
        recorded = commit(ctx, path, data, new_data, [change])
    except ProgramValidationError as exc:
        return refuse(BAD_REQUEST, "; ".join(exc.errors))

    return SkillResult(
        ok=True,
        value={"person": person, "identity": advisory, "changes": recorded},
        actions_taken=[f"added {principal} to {len(lanes) or 'no'} lane(s)"],
    )


# ---- edit -----------------------------------------------------------------


def edit(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Edit a member's name / drives / accounts. Lane and role moves go through
    ``person reassign`` so they log as a reassignment."""
    data, path, bad = load_for_edit(params, ctx)
    if bad is not None:
        return bad
    assert data is not None and path is not None
    denied = _forbidden(data, ctx)
    if denied is not None:
        return denied

    principal = params.get("principal")
    if not valid_principal(principal):
        return refuse(BAD_REQUEST, f"{principal!r} is not a principal of the form @name or @name:context")
    existing = data.person(principal)
    if existing is None:
        return refuse(ABSENT, f"{principal} is not a member of this program")

    accounts, acc_err = parse_accounts(params.get("account"))
    if acc_err is not None:
        return refuse(BAD_REQUEST, acc_err)

    new_data = mutable_copy(data)
    person = new_data.person(principal)
    assert person is not None
    before = {"name": person.get("name"), "accounts": person.get("accounts"), "drives": person.get("drives")}

    touched = False
    if params.get("name") is not None:
        person["name"] = str(params["name"])
        touched = True
    if params.get("drives") is not None:
        person["drives"] = str(params["drives"])
        touched = True
    if accounts is not None:
        # Merge: a given system overwrites; nothing is dropped unless named.
        merged = dict(person.get("accounts") or {})
        merged.update(accounts)
        person["accounts"] = merged
        touched = True

    if not touched:
        return refuse(BAD_REQUEST, "nothing to edit: pass --name, --drives, or --account")

    after = {"name": person.get("name"), "accounts": person.get("accounts"), "drives": person.get("drives")}
    change = _person_change("person_edited", principal, old=before, new=after)
    try:
        recorded = commit(ctx, path, data, new_data, [change])
    except ProgramValidationError as exc:
        return refuse(BAD_REQUEST, "; ".join(exc.errors))

    return SkillResult(
        ok=True,
        value={"person": person, "changes": recorded},
        actions_taken=[f"edited {principal}"],
    )


# ---- remove ---------------------------------------------------------------


def remove(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    data, path, bad = load_for_edit(params, ctx)
    if bad is not None:
        return bad
    assert data is not None and path is not None
    denied = _forbidden(data, ctx)
    if denied is not None:
        return denied

    principal = params.get("principal")
    if not valid_principal(principal):
        return refuse(BAD_REQUEST, f"{principal!r} is not a principal of the form @name or @name:context")
    if data.person(principal) is None:
        return refuse(ABSENT, f"{principal} is not a member of this program")

    owned = data.items_for_owner(principal)
    reassign_to = params.get("reassign_to")
    if owned and not reassign_to:
        ids = ", ".join(i.get("id", "?") for i in owned)
        return refuse(
            CONFLICT,
            f"{principal} still owns item(s) {ids}; pass --reassign-to @name:context "
            "to move them, or remove/reassign those items first",
        )
    if owned:
        if not valid_principal(reassign_to):
            return refuse(BAD_REQUEST, f"--reassign-to {reassign_to!r} is not a valid principal")
        if data.person(reassign_to) is None:
            return refuse(BAD_REQUEST, f"--reassign-to {reassign_to} is not a member of this program")

    new_data = mutable_copy(data)
    extra: list[dict[str, Any]] = []
    if owned:
        for entry in new_data.schedule:
            if entry.get("owner") == principal:
                apply_owner(new_data, entry, reassign_to)  # owner_changed logged via the snapshot diff
    new_data.raw["people"] = [p for p in new_data.people if p.get("principal") != principal]
    extra.append(_person_change("person_removed", principal, old={"principal": principal}))

    try:
        recorded = commit(ctx, path, data, new_data, extra)
    except ProgramValidationError as exc:
        return refuse(CONFLICT, "; ".join(exc.errors))

    return SkillResult(
        ok=True,
        value={"removed": principal, "reassigned_to": reassign_to if owned else None, "changes": recorded},
        actions_taken=[f"removed {principal}" + (f"; reassigned {len(owned)} item(s) to {reassign_to}" if owned else "")],
    )


# ---- reassign -------------------------------------------------------------


def reassign(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Move a member to a different set of lanes and/or change their role."""
    data, path, bad = load_for_edit(params, ctx)
    if bad is not None:
        return bad
    assert data is not None and path is not None
    denied = _forbidden(data, ctx)
    if denied is not None:
        return denied

    principal = params.get("principal")
    if not valid_principal(principal):
        return refuse(BAD_REQUEST, f"{principal!r} is not a principal of the form @name or @name:context")
    existing = data.person(principal)
    if existing is None:
        return refuse(ABSENT, f"{principal} is not a member of this program")

    change_lanes = "lane" in params and params.get("lane") is not None
    change_role = params.get("role") is not None
    if not change_lanes and not change_role:
        return refuse(BAD_REQUEST, "nothing to reassign: pass --lane (one or more) and/or --role")

    new_lanes = _lanes_param(params) if change_lanes else list(existing.get("lanes", []))
    unknown = [lane for lane in new_lanes if lane not in data.lane_ids()]
    if unknown:
        return refuse(BAD_REQUEST, f"unknown lane(s): {', '.join(unknown)}")

    old = {"lanes": list(existing.get("lanes", [])), "role": existing.get("role")}

    new_data = mutable_copy(data)
    person = new_data.person(principal)
    assert person is not None
    if change_lanes:
        person["lanes"] = new_lanes
    if change_role:
        person["role"] = str(params["role"])
    new = {"lanes": person.get("lanes", []), "role": person.get("role")}

    if new == old:
        return refuse(BAD_REQUEST, "that reassignment is a no-op (same lanes and role)")

    change = _person_change("person_reassigned", principal, old=old, new=new)
    try:
        recorded = commit(ctx, path, data, new_data, [change])
    except ProgramValidationError as exc:
        return refuse(BAD_REQUEST, "; ".join(exc.errors))

    return SkillResult(
        ok=True,
        value={"person": person, "old": old, "new": new, "changes": recorded},
        actions_taken=[f"reassigned {principal}"],
    )
