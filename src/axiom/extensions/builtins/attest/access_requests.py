# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Asking for access to a log, or for the approver role (#1867; the owner's rule of 2026-11-16).

When a person can already know a log or an entry exists another way, the node names it and offers
the request path; when they could not, it answers 404 and names nothing (unit W1-A9 stands):

- A log is **knowable** to a person at its site when it is declared ``listed``, or when they hold a
  link to one of its entries: an entry's id is a random UUID, so holding one means someone sent it.
- The answer names the log, who decides (holders of its ``access_by`` roles) and the person's own
  request. It never carries an entry's title, fields, number or signer.
- Sharing grants the log's first ``readers`` role at the site; the approver role is granted as
  itself. A refusal says why, and the person sees it as "previously refused".

The request rows are the generic ones (:mod:`axiom.governance.access.requests`); this module only
decides who may ask, who decides and what a grant gives.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.governance.access import requests as store

from . import registry
from . import roles as role_store
from .logbooks import PLATFORM_ADMIN_ROLES, Logbook, LogbookError

LOG = "logbook"
ROLE = "role"
#: The one role a person may ask for here: deciding held actions (the held card).
APPROVER = "approver"


def name_of(principal: str) -> str | None:
    """A person's name, as the site's accounts or their last signed entry give it; never a handle."""
    from axiom.webauth.users import installed_user_store

    name = principal[1:] if principal.startswith("@") else principal
    user_id, _sep, _site = name.partition(":")
    users = installed_user_store()
    if users is not None:
        user = users.get_by_id(user_id)
        if user is not None and user.name:
            return user.name
    from sqlalchemy import select

    from . import store as attest_store
    from .db_models import AttestRecord

    with attest_store.session_scope() as s:
        rec = s.execute(
            select(AttestRecord.record).where(AttestRecord.signer == principal)
            .order_by(AttestRecord.recorded_at.desc()).limit(1)
        ).scalar_one_or_none()  # fmt: skip
    display = ((rec or {}).get("signer") or {}).get("display") if isinstance(rec, dict) else None
    return display or None


def _holders(site: str, wanted: set[str], state_dir: Path) -> list[str]:
    wanted = wanted - PLATFORM_ADMIN_ROLES
    return sorted(r["principal"] for r in role_store.assignments(site, state_dir=state_dir)
                  if wanted & set(r["roles"]))  # fmt: skip


def deciders(site: str, kind: str, object_id: str, state_dir: Path) -> list[str]:
    if kind == LOG:
        try:
            book = registry.get(object_id)
        except LogbookError:
            return []
        return _holders(site, set(book.access_by), state_dir)
    if kind == ROLE and object_id == APPROVER:
        return _holders(site, {APPROVER}, state_dir)
    return []


def _named(principals: list[str], *, but: str | None = None) -> list[dict[str, Any]]:
    return [{"name": name_of(p)} for p in principals if p != but]


def knowable(person: str, site: str, book: Logbook, record_id: str | None) -> bool:
    """Whether the person can already know this log exists: it is listed, or they hold a link to an
    entry that is in it at their site."""
    if book.listed:
        return True
    if not record_id:
        return False
    from . import service

    rec = service.record(record_id)
    return rec is not None and rec["logbook"] == book.id and rec["site_id"] == site


def own_view(person: str, kind: str, object_id: str) -> dict[str, Any]:
    """The person's open request and last refusal, the refuser named, never a handle."""
    mine = store.mine(kind=kind, object_id=object_id, requester=person)
    refused = mine["previously_refused"]
    if refused:
        mine["previously_refused"] = {**refused, "by": name_of(refused["by"]) if refused.get("by") else None}
    if mine["open"]:
        mine["open"] = _public(mine["open"])
    return mine


def _public(req: dict[str, Any]) -> dict[str, Any]:
    """A request as its requester reads it: no handles."""
    out = {k: v for k, v in req.items() if k not in ("requester", "decided_by")}
    if req.get("decided_by"):
        out["decided_by_name"] = name_of(req["decided_by"])
    return out


def no_access(person: str, site: str, book: Logbook, state_dir: Path) -> dict[str, Any]:
    """The 403 body for a log the person may not read but can know of: its name, who decides, and
    their own request. Nothing about any entry."""
    return {
        "detail": f"{book.display} is not open to you",
        "code": "no_access",
        "logbook": {"id": book.id, "display": book.display},
        "deciders": _named(deciders(site, LOG, book.id, state_dir), but=person),
        "request": own_view(person, LOG, book.id),
    }


def ask_for_log(person: str, site: str, book: Logbook, note: str, state_dir: Path) -> dict[str, Any]:
    role = book.readers[0] if book.readers else "reader"
    req = store.ask(site=site, kind=LOG, object_id=book.id, requester=person, role=role, note=note)
    return {**_public(req), "sent_to": _named(deciders(site, LOG, book.id, state_dir), but=person)}


def ask_for_role(person: str, site: str, note: str, state_dir: Path) -> dict[str, Any]:
    req = store.ask(site=site, kind=ROLE, object_id=APPROVER, requester=person, role=APPROVER, note=note)
    return {**_public(req), "sent_to": _named(deciders(site, ROLE, APPROVER, state_dir), but=person)}


def _objects_decided_by(person: str, site: str, state_dir: Path) -> list[tuple[str, str]]:
    held = set(role_store.roles_for(person, site, state_dir=state_dir)) - PLATFORM_ADMIN_ROLES
    out = [(LOG, b.id) for b in registry.all_logbooks() if held & set(b.access_by)]
    if APPROVER in held:
        out.append((ROLE, APPROVER))
    return out


def _title(req: dict[str, Any]) -> str:
    who = name_of(req["requester"]) or "Someone at the site"
    kind, oid = req["object"]["kind"], req["object"]["id"]
    if kind == ROLE:
        return f"{who} asks for the {oid} role"
    try:
        display = registry.get(oid).display
    except LogbookError:
        display = oid
    return f"{who} asks to read {display}"


def to_answer(person: str, site: str, state_dir: Path) -> list[dict[str, Any]]:
    """The open requests this person decides, each with the requester's name and what it is for."""
    reqs = store.open_for(site=site, objects=_objects_decided_by(person, site, state_dir))
    return [{**r, "requester_name": name_of(r["requester"]), "title": _title(r)}
            for r in reqs if r["requester"] != person]  # fmt: skip


def answer(person: str, request_id: str, *, approve: bool, reason: str, state_dir: Path) -> dict[str, Any]:
    """Share (grant the role the request names) or decline with a reason. Only a decider answers;
    anyone else is told there is no such request."""
    req = store.get(request_id)
    if req is None or req["requester"] == person:
        raise store.RequestNotFound(f"no access request {request_id}")
    site = req["site"]
    target = (req["object"]["kind"], req["object"]["id"])
    if target not in _objects_decided_by(person, site, state_dir):
        raise store.RequestNotFound(f"no access request {request_id}")
    if not approve:
        return store.settle(request_id, by=person, approve=False, reason=reason)
    if req["state"] != "open":
        raise store.RequestRefused(f"that request is already {req['state']}")
    role = req["role"]
    role_store.grant(req["requester"], site, role, by=person, state_dir=state_dir)
    return store.settle(request_id, by=person, approve=True, granted_role=role)


def queue_items(person: str, sites: list[str], state_dir: Path) -> list[dict[str, Any]]:
    """Needs you: one "due" item per request this person decides, linking to where it is answered."""
    out = []
    for site in sites:
        for r in to_answer(person, site, state_dir):
            kind, oid = r["object"]["kind"], r["object"]["id"]
            href = f"#/log?logbook={oid}&access={r['id']}" if kind == LOG else f"#/control?access={r['id']}"
            out.append({
                "id": f"attest:access:{r['id']}", "section": "due", "kind": "access", "title": r["title"],
                "source": "attest", "due": None, "priority": "normal", "href": href, "action": None,
                "created_at": r.get("created_at"),
            })  # fmt: skip
    return out


__all__ = [
    "APPROVER",
    "LOG",
    "ROLE",
    "answer",
    "ask_for_log",
    "ask_for_role",
    "deciders",
    "knowable",
    "name_of",
    "no_access",
    "own_view",
    "queue_items",
    "to_answer",
]
