# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The share acts: share, remove, general access, settings, transfer, requests (SHR-R2 to R6, R10).

Each act asks :func:`.decision.require` first, so it is a ``decide()`` receipt
whether it is allowed or refused (SHR-R10.1, SHR-A16), then writes the record.
What the object's home must do afterwards (log the act in the item's activity,
move its retrieval, change its owner column) it does with the :class:`Change`
returned; nothing here reaches into another extension's tables.

A share target is a person or a group at the object's site, never anyone else
(SHR-R2.2): a person is named ``@name:<site>``, a group ``group:<id>``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

from axiom.governance.access import store
from axiom.governance.access.decision import (
    AccessObject,
    NoAccess,
    Refused,
    Who,
    require,
    role_of,
    settings_for,
)
from axiom.governance.access.roles import GRANTABLE, GROUP_PREFIX, RESTRICTED, SITE, Role

#: One reminder per 24 hours (SHR-R6.3).
REMIND_EVERY = timedelta(hours=24)


@dataclass(frozen=True)
class Change:
    """What one act changed, for the home's activity line and projection."""

    act: str
    by: str
    receipt_id: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"act": self.act, "by": self.by, "receipt_id": self.receipt_id, **self.detail}


def _site_of(handle: str) -> str | None:
    h = handle[1:] if handle.startswith("@") else handle
    _name, sep, context = h.partition(":")
    return context.strip().lower() or None if sep else None


def clean_subject(raw: str, site: str) -> str:
    """A share target at ``site``: ``@name:<site>`` or ``group:<id>``, or a refusal saying why."""
    text = " ".join(str(raw or "").split())
    if not text:
        raise ValueError("say who to share with: @name:<site> or group:<id>")
    if text.lower().startswith(GROUP_PREFIX):
        gid = text[len(GROUP_PREFIX):].strip()
        if not gid:
            raise ValueError("name the group: group:<id>")
        return f"{GROUP_PREFIX}{gid}"
    if not text.startswith("@"):
        raise ValueError(f"{text!r} is not a person or a group: use @name:{site} or group:<id>")
    other = _site_of(text)
    if other is None:
        raise ValueError(f"name the person with their site, as {text}:{site}")
    if other != site:
        raise ValueError("Nothing here is shared outside its site; that person is at another site.")
    return text


def _clean_role(kind: str, raw: object) -> Role:
    role = Role.parse(raw)
    if role not in GRANTABLE.get(kind, ()):
        if role is Role.OWNER:
            raise ValueError("ownership is transferred, not shared: use transfer")
        allowed = ", ".join(r.value for r in GRANTABLE.get(kind, ()))
        raise ValueError(f"a {kind} can be shared as {allowed}, not {role.value}")
    return role


def share(person: Who, obj: AccessObject, subject: str, role: object, *, expires: object = None,
          surface: str = "", via: str = "share") -> Change:
    """Give ``subject`` ``role`` on ``obj`` (an earlier entry for them is replaced)."""
    target = clean_subject(subject, obj.site)
    granted = _clean_role(obj.kind, role)
    expires_at = store.clean_expiry(expires)
    if obj.held:
        raise Refused("Sharing waits for the review: this file is held for export-control review.",
                      role=None, needed="a release")
    if target == obj.owner:
        raise ValueError("that person owns it already")
    with store.session_scope() as s:
        decision = require(person, "library.share", obj, session=s, surface=surface)
        new, replaced = store.add_grant(
            s, site=obj.site, kind=obj.kind, object_id=obj.id, subject=target, role=granted, expires_at=expires_at,
            by=person.principal, via=via, receipt_id=decision.receipt_id,
        )
        s.commit()
    detail: dict[str, Any] = {"subject": target, "role": granted.value, "grant_id": new.id}
    if expires_at is not None:
        detail["expires_at"] = expires_at.isoformat()
    if replaced is not None:
        detail["was"] = replaced.role.value
    return Change(act="share", by=person.principal, receipt_id=decision.receipt_id, detail=detail)


def unshare(person: Who, obj: AccessObject, subject: str, *, surface: str = "") -> Change:
    target = clean_subject(subject, obj.site)
    with store.session_scope() as s:
        decision = require(person, "library.share", obj, session=s, surface=surface)
        ended = store.revoke_grants(s, kind=obj.kind, object_id=obj.id, subject=target, by=person.principal)
        if not ended:
            raise LookupError(f"{target} has no access of their own to remove")
        s.commit()
    return Change(act="unshare", by=person.principal, receipt_id=decision.receipt_id,
                  detail={"subject": target, "was": ended[0].role.value})


def set_general(person: Who, obj: AccessObject, general: str, role: object = Role.VIEWER, *,
                surface: str = "") -> Change:
    """Restricted, or Everyone at the site with ``role`` (SHR-R2.1)."""
    line = (general or "").strip().lower()
    if line not in (RESTRICTED, SITE):
        raise ValueError("general access is 'restricted' or 'site' (Everyone at the site)")
    granted = _clean_role(obj.kind, role) if line == SITE else Role.VIEWER
    if obj.held and line == SITE:
        raise Refused("Sharing waits for the review: this file is held for export-control review.",
                      role=None, needed="a release")
    with store.session_scope() as s:
        decision = require(person, "library.share", obj, session=s, surface=surface)
        before = settings_for(obj, s)
        store.write_settings(s, kind=obj.kind, object_id=obj.id, site=obj.site,
                             settings=replace(before, general=line, general_role=granted), by=person.principal)
        s.commit()
    return Change(act="general", by=person.principal, receipt_id=decision.receipt_id, detail={
        "general": line, "role": granted.value if line == SITE else None,
        "was": before.general, "was_role": before.general_role.value if before.general == SITE else None,
    })


def set_settings(person: Who, obj: AccessObject, *, editors_can_share: bool | None = None,
                 viewers_can_download: bool | None = None, surface: str = "") -> Change:
    """The Owner's two Google settings (ADR-263 D3)."""
    if editors_can_share is None and viewers_can_download is None:
        raise ValueError("say which setting to change")
    with store.session_scope() as s:
        decision = require(person, "library.settings", obj, session=s, surface=surface)
        before = settings_for(obj, s)
        after = replace(
            before,
            editors_can_share=before.editors_can_share if editors_can_share is None else bool(editors_can_share),
            viewers_can_download=before.viewers_can_download if viewers_can_download is None else bool(viewers_can_download),
        )
        store.write_settings(s, kind=obj.kind, object_id=obj.id, site=obj.site, settings=after, by=person.principal)
        s.commit()
    return Change(act="settings", by=person.principal, receipt_id=decision.receipt_id, detail={
        "editors_can_share": after.editors_can_share, "viewers_can_download": after.viewers_can_download,
    })


def transfer(person: Who, obj: AccessObject, new_owner: str, *, surface: str = "") -> Change:
    """Make someone already on the access list the Owner; the old Owner becomes Editor (SHR-R5.1).

    The home changes its owner column with ``detail['owner']``; this writes the
    old Owner's Editor entry and ends the new Owner's own entry, which ownership
    now covers.
    """
    target = clean_subject(new_owner, obj.site)
    if target.startswith(GROUP_PREFIX):
        raise ValueError("an Owner is always a person, never a group")
    if target == obj.owner:
        raise ValueError("that person owns it already")
    with store.session_scope() as s:
        decision = require(person, "library.transfer", obj, session=s, surface=surface)
        listed = store.grants_on(s, [obj.key], subjects=[target])
        if not listed:
            raise ValueError("ownership goes to someone already on the access list: share it with them first")
        store.revoke_grants(s, kind=obj.kind, object_id=obj.id, subject=target, by=person.principal, reason="replaced")
        store.add_grant(s, site=obj.site, kind=obj.kind, object_id=obj.id, subject=obj.owner, role=Role.EDITOR,
                        expires_at=None, by=person.principal, via="transfer", receipt_id=decision.receipt_id)
        s.commit()
    return Change(act="transfer", by=person.principal, receipt_id=decision.receipt_id,
                  detail={"owner": target, "was": obj.owner})


# ------------------------------------------------------------------- who has access


def access_list(person: Who, obj: AccessObject, *, surface: str = "") -> dict[str, Any]:
    """Who has access and why (SHR-R3.1, R3.3): the Owner, each entry, general access, the settings.

    A Viewer who is not on the list sees only the Owner and the general access
    line, not the other people (as Google).
    """
    with store.session_scope() as s:
        decision = require(person, "library.access", obj, session=s, surface=surface)
        settings = settings_for(obj, s)
        entries = store.grants_on(s, [obj.key])
        role = decision.role
    on_list = any(g.subject in person.subjects for g in entries)
    shows_people = role is not None and (role.at_least(Role.EDITOR) or on_list)
    out: dict[str, Any] = {
        "object": {"kind": obj.kind, "id": obj.id},
        "owner": obj.owner,
        "your_role": role.value if role else None,
        "general": settings.to_json(),
        "held": obj.held,
        "people": [g.to_json() for g in entries] if shows_people else None,
        "inherited_from": [p.id for p in obj.parents],
    }
    if obj.held:
        out["note"] = "Sharing waits for the review."
    if not shows_people:
        out["hidden"] = "Only the owner, editors and the people listed see who else has access."
    return out


# ------------------------------------------------------------------------- requests


def _request_json(row: Any) -> dict[str, Any]:
    def iso(v: Any) -> str | None:
        return store._aware(v).isoformat() if v is not None else None  # noqa: SLF001

    return {
        "id": row.id, "object": {"kind": row.object_kind, "id": row.object_id}, "requester": row.requester,
        "role": row.role, "note": row.note, "state": row.state, "created_at": iso(row.created_at),
        "reminded_at": iso(row.reminded_at), "decided_by": row.decided_by, "decided_at": iso(row.decided_at),
        "granted_role": row.granted_role, "reason": row.reason,
    }


def request_access(person: Who, obj: AccessObject, role: object = Role.VIEWER, *, note: str = "",
                   surface: str = "") -> dict[str, Any]:
    """Ask the Owner for ``role`` (SHR-R6.1). Answers what was sent; names nothing about the object.

    A person who already has the role is told so; an open request is returned,
    not doubled. A previous refusal does not stop a new request (Request again).
    """
    from axiom.extensions.builtins.authz.db_models import AccessRequest

    asked = _clean_role(obj.kind, role)
    with store.session_scope() as s:
        require(person, "library.request", obj, session=s, surface=surface)
        current = role_of(person, obj, session=s)
        if current is not None and current.at_least(asked):
            raise ValueError(f"you already have {current.value} access")
        open_row = (
            s.query(AccessRequest)
            .filter(AccessRequest.object_kind == obj.kind, AccessRequest.object_id == obj.id,
                    AccessRequest.requester == person.principal, AccessRequest.state == "open")
            .one_or_none()
        )
        if open_row is not None:
            return {**_request_json(open_row), "already_open": True}
        row = AccessRequest(
            id=str(uuid.uuid4()), site=obj.site, object_kind=obj.kind, object_id=obj.id, requester=person.principal,
            role=asked.value, note=" ".join(str(note or "").split())[:2000], state="open", created_at=store.now(),
        )
        s.add(row)
        s.commit()
        return {**_request_json(row), "sent_to": "the owner"}


def _open_request(s: Any, obj: AccessObject, request_id: str) -> Any:
    from axiom.extensions.builtins.authz.db_models import AccessRequest

    row = s.get(AccessRequest, request_id)
    if row is None or row.object_kind != obj.kind or row.object_id != obj.id:
        raise LookupError(f"no access request {request_id!r} on this {obj.kind}")
    if row.state != "open":
        raise ValueError(f"that request is already {row.state}")
    return row


def answer_request(person: Who, obj: AccessObject, request_id: str, *, approve: bool, role: object = None,
                   reason: str = "", surface: str = "") -> tuple[dict[str, Any], Change | None]:
    """Share (with the role chosen, which may be lower than asked) or Decline with a reason (SHR-R6.2).

    Whoever may share answers. A request grants nothing until it is answered, and
    the answer is the recorded share (SHR-R6.5).
    """
    with store.session_scope() as s:
        row = _open_request(s, obj, request_id)
        requester, asked = row.requester, row.role
    if approve:
        granted = _clean_role(obj.kind, role or asked)
        change = share(person, obj, requester, granted, surface=surface, via="request")
        with store.session_scope() as s:
            row = _open_request(s, obj, request_id)
            row.state, row.decided_by, row.decided_at, row.granted_role = (
                "approved", person.principal, store.now(), granted.value)
            s.commit()
            return _request_json(row), change
    why = " ".join(str(reason or "").split())
    if not why:
        raise ValueError("say why, in a few words: the requester sees it as written")
    with store.session_scope() as s:
        decision = require(person, "library.share", obj, session=s, surface=surface)
        row = _open_request(s, obj, request_id)
        row.state, row.decided_by, row.decided_at, row.reason = "declined", person.principal, store.now(), why
        s.commit()
        return _request_json(row), Change(act="decline", by=person.principal, receipt_id=decision.receipt_id,
                                          detail={"requester": requester, "role": asked, "reason": why})


def withdraw_request(person: Who, obj: AccessObject, request_id: str) -> dict[str, Any]:
    with store.session_scope() as s:
        row = _open_request(s, obj, request_id)
        if row.requester != person.principal:
            raise NoAccess("You need access. Ask for it, and the owner is told.")
        row.state = "withdrawn"
        s.commit()
        return _request_json(row)


def remind_request(person: Who, obj: AccessObject, request_id: str) -> dict[str, Any]:
    """One reminder per 24 hours (SHR-R6.3)."""
    with store.session_scope() as s:
        row = _open_request(s, obj, request_id)
        if row.requester != person.principal:
            raise NoAccess("You need access. Ask for it, and the owner is told.")
        last = store._aware(row.reminded_at or row.created_at)  # noqa: SLF001
        if last is not None and store.now() - last < REMIND_EVERY:
            raise ValueError("one reminder a day: the owner was asked less than 24 hours ago")
        row.reminded_at = store.now()
        s.commit()
        return _request_json(row)


def requests_for(person: Who, obj: AccessObject, *, state: str | None = "open") -> list[dict[str, Any]]:
    """The requests on ``obj``, for someone who may answer them (the share dialog's banner)."""
    from axiom.extensions.builtins.authz.db_models import AccessRequest

    with store.session_scope() as s:
        require(person, "library.share", obj, session=s)
        q = s.query(AccessRequest).filter(AccessRequest.object_kind == obj.kind, AccessRequest.object_id == obj.id)
        if state:
            q = q.filter(AccessRequest.state == state)
        return [_request_json(r) for r in q.order_by(AccessRequest.created_at).all()]


def my_requests(person: Who, obj: AccessObject) -> dict[str, Any]:
    """The requester's own view: what is open, and the last refusal as written (SHR-R6.3, 'previously refused')."""
    from axiom.extensions.builtins.authz.db_models import AccessRequest

    with store.session_scope() as s:
        rows = (
            s.query(AccessRequest)
            .filter(AccessRequest.object_kind == obj.kind, AccessRequest.object_id == obj.id,
                    AccessRequest.requester == person.principal)
            .order_by(AccessRequest.created_at.desc())
            .all()
        )
        opened = next((r for r in rows if r.state == "open"), None)
        declined = next((r for r in rows if r.state == "declined"), None)
        return {
            "open": _request_json(opened) if opened else None,
            "previously_refused": (
                {"by": declined.decided_by, "at": _request_json(declined)["decided_at"], "reason": declined.reason}
                if declined else None
            ),
        }


__all__ = [
    "Change",
    "REMIND_EVERY",
    "access_list",
    "answer_request",
    "clean_subject",
    "my_requests",
    "remind_request",
    "request_access",
    "requests_for",
    "set_general",
    "set_settings",
    "share",
    "transfer",
    "unshare",
    "withdraw_request",
]
