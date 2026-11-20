# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A request for access to anything that is not a Library item (#1867): a log, a role.

The Library's share acts (:mod:`.sharing`) decide through the Library's own verbs. Other homes
decide by their own rules (who may read a log, who holds a role) and keep the request here, in the
same ``access_requests`` table and the same shape, so a request reads the same everywhere: open,
withdrawn, approved or declined, with who answered, when, and why.

Nothing here decides who may ask or who may answer: the caller does, before calling. Each function
is one read or one write of the request row.
"""

from __future__ import annotations

import uuid
from typing import Any

from axiom.governance.access import store
from axiom.governance.access.sharing import REMIND_EVERY, _request_json


class RequestNotFound(LookupError):
    """No such request, or not one this caller may act on: the two read the same."""


class RequestRefused(ValueError):
    """The act is not possible now (already answered, reminded today); the message says why."""


def _row(s: Any, request_id: str) -> Any:
    from axiom.extensions.builtins.authz.db_models import AccessRequest

    return s.get(AccessRequest, request_id)


def ask(*, site: str, kind: str, object_id: str, requester: str, role: str, note: str = "") -> dict[str, Any]:
    """Open a request, or return the open one (``already_open``): a second ask never doubles it."""
    from axiom.extensions.builtins.authz.db_models import AccessRequest

    with store.session_scope() as s:
        open_row = (
            s.query(AccessRequest)
            .filter(AccessRequest.object_kind == kind, AccessRequest.object_id == object_id,
                    AccessRequest.requester == requester, AccessRequest.state == "open")
            .first()
        )  # fmt: skip
        if open_row is not None:
            return {**_request_json(open_row), "already_open": True}
        row = AccessRequest(
            id=str(uuid.uuid4()), site=site, object_kind=kind, object_id=object_id, requester=requester,
            role=role, note=" ".join(str(note or "").split())[:2000], state="open", created_at=store.now(),
        )  # fmt: skip
        s.add(row)
        s.commit()
        return _request_json(row)


def mine(*, kind: str, object_id: str, requester: str) -> dict[str, Any]:
    """The requester's own view: the open request, and the last refusal as written."""
    from axiom.extensions.builtins.authz.db_models import AccessRequest

    with store.session_scope() as s:
        rows = (
            s.query(AccessRequest)
            .filter(AccessRequest.object_kind == kind, AccessRequest.object_id == object_id,
                    AccessRequest.requester == requester)
            .order_by(AccessRequest.created_at.desc())
            .all()
        )  # fmt: skip
        opened = next((r for r in rows if r.state == "open"), None)
        declined = next((r for r in rows if r.state == "declined"), None)
        return {
            "open": _request_json(opened) if opened else None,
            "previously_refused": (
                {"by": declined.decided_by, "at": _request_json(declined)["decided_at"], "reason": declined.reason}
                if declined
                else None
            ),
        }


def get(request_id: str) -> dict[str, Any] | None:
    with store.session_scope() as s:
        row = _row(s, request_id)
        return None if row is None else {**_request_json(row), "site": row.site}


def open_for(*, site: str, objects: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """The open requests at ``site`` on any of ``objects`` (``(kind, id)`` pairs), oldest first."""
    from axiom.extensions.builtins.authz.db_models import AccessRequest

    if not objects:
        return []
    with store.session_scope() as s:
        rows = (
            s.query(AccessRequest)
            .filter(AccessRequest.site == site, AccessRequest.state == "open")
            .order_by(AccessRequest.created_at)
            .all()
        )
        wanted = set(objects)
        return [_request_json(r) for r in rows if (r.object_kind, r.object_id) in wanted]


def _own_open(s: Any, request_id: str, requester: str) -> Any:
    row = _row(s, request_id)
    if row is None or row.requester != requester:
        raise RequestNotFound(f"no access request {request_id}")
    if row.state != "open":
        raise RequestRefused(f"that request is already {row.state}")
    return row


def withdraw(request_id: str, *, requester: str) -> dict[str, Any]:
    with store.session_scope() as s:
        row = _own_open(s, request_id, requester)
        row.state = "withdrawn"
        s.commit()
        return _request_json(row)


def remind(request_id: str, *, requester: str) -> dict[str, Any]:
    """One reminder per 24 hours."""
    with store.session_scope() as s:
        row = _own_open(s, request_id, requester)
        last = store._aware(row.reminded_at or row.created_at)  # noqa: SLF001
        if last is not None and store.now() - last < REMIND_EVERY:
            raise RequestRefused("one reminder a day: they were asked less than 24 hours ago")
        row.reminded_at = store.now()
        s.commit()
        return _request_json(row)


def settle(request_id: str, *, by: str, approve: bool, granted_role: str | None = None,
           reason: str = "") -> dict[str, Any]:  # fmt: skip
    """Record the answer. A refusal says why, in the words the requester will read."""
    why = " ".join(str(reason or "").split())
    if not approve and not why:
        raise RequestRefused("say why, in a few words: the person who asked sees it as written")
    with store.session_scope() as s:
        row = _row(s, request_id)
        if row is None:
            raise RequestNotFound(f"no access request {request_id}")
        if row.state != "open":
            raise RequestRefused(f"that request is already {row.state}")
        row.decided_by, row.decided_at = by, store.now()
        if approve:
            row.state, row.granted_role = "approved", granted_role or row.role
        else:
            row.state, row.reason = "declined", why
        s.commit()
        return _request_json(row)


__all__ = ["RequestNotFound", "RequestRefused", "ask", "get", "mine", "open_for", "remind", "settle", "withdraw"]
