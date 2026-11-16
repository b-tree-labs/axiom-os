# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Sharing a Library item or folder: the acts, with what only the Library adds (prd-sharing-and-access).

The record and the verdicts are :mod:`axiom.governance.access`'s; this adds the
Library's part of each act:

- the item's **activity**, in words ("@ana:alpha gave @cy:alpha edit access"),
  as a ``library_events`` row (SHR-R10.1);
- the **projection**: general access writes ``visibility`` in step and moves the
  item's chunks to the corpus its audience reads (SHR-R2.4); a transfer moves a
  Restricted item's chunks to the new Owner's corpus;
- the **clearance** check: a share to a person whose clearance does not cover the
  item's confirmed label is refused, naming the label and not the content (SHR-R8.1);
- **notification** through HERALD: the person added (when "Notify people" is on),
  the Owner when someone asks (their Needs you), the requester when answered.

A target is an item id, or ``folder:<path>`` for a person's folder. Commenter is
stored by the model and offered once comments exist (decision 16).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from axiom.extensions.builtins.library import access
from axiom.extensions.builtins.library.scope import Caller
from axiom.extensions.builtins.library.store import session_scope
from axiom.governance.access import NoAccess, Refused, Role
from axiom.governance.access import sharing as acts
from axiom.governance.access.roles import FOLDER, GROUP_PREFIX, RESTRICTED, SITE

log = logging.getLogger(__name__)

FOLDER_PREFIX = "folder:"

#: Offered once comments exist (prd-sharing-and-access decision 16).
NOT_YET = {Role.COMMENTER: "Commenter is offered once comments exist; share as viewer or editor."}

Notifier = Callable[[str, str, str, str], Any]
_notifier: Notifier | None = None


def set_notifier(fn: Notifier | None) -> None:
    """Bind how people are told (tests); ``None`` sends through HERALD."""
    global _notifier
    _notifier = fn


def _herald(recipient: str, summary: str, body: str, dedup: str) -> Any:
    from axiom.extensions.builtins.notifications.send import NotificationPayload, Priority, send
    from axiom.extensions.builtins.notifications.skills.send import _ctx
    from axiom.governance import Classification

    return send(
        _ctx(), actor="@library", recipient=recipient,
        payload=NotificationPayload(summary=summary, body=body, metadata={"kind": "library_access"}),
        classification=Classification.INTERNAL, priority=Priority.NORMAL, intent="library.access",
        dedup_key=dedup,
    )


def _tell(recipient: str, summary: str, body: str = "", dedup: str = "") -> bool:
    """Tell one person; a failed notice is logged, never raised, and never undoes the act."""
    if not recipient or recipient.startswith(GROUP_PREFIX):
        return False
    try:
        (_notifier or _herald)(recipient, summary, body, dedup or summary)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("library access notice to %s failed: %s", recipient, type(exc).__name__)
        return False


# ------------------------------------------------------------------ the target


class _Target:
    def __init__(self, kind: str, row: Any, obj: Any, folders: dict[str, Any]) -> None:
        self.kind, self.row, self.obj, self.folders = kind, row, obj, folders

    @property
    def event_id(self) -> str:
        return self.row.id

    @property
    def name(self) -> str:
        return getattr(self.row, "title", None) or getattr(self.row, "label", "") or self.row.id


def _target(session: Any, caller: Caller, ref: str) -> _Target:
    """The item or person's folder ``ref`` names, at the caller's site, or not found."""
    from axiom.extensions.builtins.library.db_models import LibraryItem
    from axiom.extensions.builtins.library.service import NotFound

    ref = (ref or "").strip()
    if not ref:
        raise ValueError("name the file (its id) or the folder (folder:<path>)")
    folders = access.user_folders(session, caller.site)
    if ref.startswith(FOLDER_PREFIX):
        row = folders.get(ref[len(FOLDER_PREFIX):].strip("/"))
        if row is None:
            raise NotFound(access.NO_ACCESS)
        return _Target(FOLDER, row, access.folder_object(row, folders), folders)
    row = session.get(LibraryItem, ref)
    if row is None or row.site != caller.site or row.deleted_at is not None:
        raise NotFound(access.NO_ACCESS)
    return _Target("document", row, access.item_object(row, folders), folders)


def _event(session: Any, caller: Caller, target: _Target, action: str, words: str, **detail: Any) -> None:
    from axiom.extensions.builtins.library.db_models import LibraryEvent
    from axiom.extensions.builtins.library.service import _now

    session.add(LibraryEvent(item_id=target.event_id, site=caller.site, principal=caller.principal,
                             action=action, surface=caller.surface, detail={"words": words, **detail}, at=_now()))


def _library_errors(fn: Callable[[], Any]) -> Any:
    from axiom.extensions.builtins.library.service import Forbidden, NotFound

    try:
        return fn()
    except NoAccess as exc:
        raise NotFound(access.NO_ACCESS) from exc
    except Refused as exc:
        raise Forbidden(str(exc)) from exc


def _role(raw: object) -> Role:
    role = Role.parse(raw or "viewer")
    if role in NOT_YET:
        raise ValueError(NOT_YET[role])
    return role


def _check_clearance(target: _Target, subject: str) -> None:
    """SHR-R8.1: never a share that lets a person read above their clearance (named by label, not content)."""
    if target.kind != "document" or subject.startswith(GROUP_PREFIX):
        return
    from axiom.extensions.builtins.library import labels
    from axiom.governance.classification import clearance_for, permits

    label = labels.confirmed(target.row)
    if label is not None and not permits(clearance_for(subject), label):
        raise PermissionError(f"{subject} is not cleared for files labelled {label.classification.value}")


# ------------------------------------------------------------------------- acts


def share(caller: Caller, ref: str, subject: str, role: object = "viewer", *, expires: object = None,
          notify: bool = True, message: str = "") -> dict[str, Any]:
    """Add a person or group with a role (SHR-R3.2); it happens at once and is recorded."""
    granted = _role(role)
    with session_scope() as session:
        target = _target(session, caller, ref)
        who = acts.clean_subject(subject, caller.site)
        _check_clearance(target, who)
        change = _library_errors(lambda: acts.share(access.who_of(caller), target.obj, who, granted,
                                                    expires=expires, surface=caller.surface))
        words = f"{caller.principal} gave {who} {granted.value} access"
        if change.detail.get("expires_at"):
            words += f" until {change.detail['expires_at'][:10]}"
        _event(session, caller, target, "share", words, **change.detail, receipt_id=change.receipt_id)
        session.commit()
    told = False
    if notify:
        body = " ".join(str(message or "").split())[:2000]
        told = _tell(who, f"{caller.principal} shared “{target.name}” with you ({granted.value})", body,
                     f"share:{change.detail['grant_id']}")
    return {**change.to_json(), "words": words, "notified": told}


def unshare(caller: Caller, ref: str, subject: str) -> dict[str, Any]:
    with session_scope() as session:
        target = _target(session, caller, ref)
        change = _library_errors(lambda: acts.unshare(access.who_of(caller), target.obj, subject,
                                                      surface=caller.surface))
        words = f"{caller.principal} removed {change.detail['subject']}'s access"
        _event(session, caller, target, "unshare", words, **change.detail, receipt_id=change.receipt_id)
        session.commit()
    return {**change.to_json(), "words": words}


def parse_general(raw: str) -> tuple[str, Role]:
    """``restricted``, ``site`` or ``site:<role>`` (SHR-R3.5)."""
    text = (raw or "").strip().lower()
    line, _, role = text.partition(":")
    if line not in (RESTRICTED, SITE):
        raise ValueError("general access is 'restricted', 'site' or 'site:<role>'")
    if line == RESTRICTED and role:
        raise ValueError("Restricted has no role: only the people listed can open it")
    return line, _role(role or "viewer")


def general(caller: Caller, ref: str, raw: str) -> dict[str, Any]:
    """Restricted, or Everyone at the site with a role; ``visibility`` and retrieval follow (SHR-R2.4)."""
    from axiom.extensions.builtins.library import service

    line, role = parse_general(raw)
    with session_scope() as session:
        target = _target(session, caller, ref)
        change = _library_errors(lambda: acts.set_general(access.who_of(caller), target.obj, line, role,
                                                          surface=caller.surface))
        if target.kind == "document":
            service._project_visibility(target.row, access.visibility_for(line), caller)  # noqa: SLF001
        words = (f"{caller.principal} made it Restricted" if line == RESTRICTED
                 else f"{caller.principal} gave everyone at {caller.site} {role.value} access")
        _event(session, caller, target, "general", words, **change.detail, receipt_id=change.receipt_id)
        session.commit()
    return {**change.to_json(), "words": words}


def settings(caller: Caller, ref: str, *, editors_can_share: bool | None = None,
             viewers_can_download: bool | None = None) -> dict[str, Any]:
    with session_scope() as session:
        target = _target(session, caller, ref)
        change = _library_errors(lambda: acts.set_settings(
            access.who_of(caller), target.obj, editors_can_share=editors_can_share,
            viewers_can_download=viewers_can_download, surface=caller.surface))
        words = f"{caller.principal} changed its sharing settings"
        _event(session, caller, target, "settings", words, **change.detail, receipt_id=change.receipt_id)
        session.commit()
    return {**change.to_json(), "words": words}


def who_has_access(caller: Caller, ref: str) -> dict[str, Any]:
    """``axi library access``: the Owner, the people (when the caller may see them), general access, why."""
    with session_scope() as session:
        target = _target(session, caller, ref)
        out = _library_errors(lambda: acts.access_list(access.who_of(caller), target.obj, surface=caller.surface))
    out["name"] = target.name
    out["may_share"] = access._decision.may(access.who_of(caller), "library.share", target.obj)  # noqa: SLF001
    if not out["may_share"]:
        settings_now = out["general"]
        out["why_not"] = ("The owner has turned off sharing by editors." if out["your_role"] == "editor"
                          and not settings_now["editors_can_share"] else "Only the owner and editors can share.")
    return out


def transfer(caller: Caller, ref: str, to: str) -> dict[str, Any]:
    """Make someone on the list the Owner; the old Owner becomes Editor; it stays where it is filed (SHR-R5)."""
    from axiom.extensions.builtins.library import service
    from axiom.extensions.builtins.library.scope import make_caller

    with session_scope() as session:
        target = _target(session, caller, ref)
        change = _library_errors(lambda: acts.transfer(access.who_of(caller), target.obj, to, surface=caller.surface))
        new_owner = change.detail["owner"]
        if target.kind == "document":
            item = target.row
            item.owner = new_owner
            if item.visibility != "site":
                # A Restricted item's chunks live in its Owner's own corpus: they move with it.
                service._unindex(item)  # noqa: SLF001
                item.corpus = service.corpus_for(make_caller(new_owner, item.site), "private")
                service._reindex_from_bronze(item)  # noqa: SLF001
        else:
            target.row.created_by = new_owner
        words = f"{caller.principal} made {new_owner} the owner"
        _event(session, caller, target, "transfer", words, **change.detail, receipt_id=change.receipt_id)
        session.commit()
    _tell(new_owner, f"{caller.principal} made you the owner of “{target.name}”", "", f"transfer:{change.receipt_id}")
    return {**change.to_json(), "words": words}


# --------------------------------------------------------------------- requests


def request(caller: Caller, ref: str, role: object = "viewer", *, note: str = "") -> dict[str, Any]:
    """Ask for access (SHR-R6.1). Works on an address the person cannot open; the answer names nothing."""
    asked = _role(role)
    with session_scope() as session:
        target = _target(session, caller, ref)
        out = _library_errors(lambda: acts.request_access(access.who_of(caller), target.obj, asked, note=note,
                                                          surface=caller.surface))
        owner = target.obj.owner
    if not out.get("already_open"):
        # The Owner's Needs you (their inbox), which says who asks and for what, not to the asker.
        _tell(owner, f"{caller.principal} asks for {asked.value} access to “{target.name}”",
              out.get("note") or "", f"access-request:{out['id']}")
    out.pop("object", None)
    return {**out, "sent_to": "the owner"}


def my_request(caller: Caller, ref: str) -> dict[str, Any]:
    with session_scope() as session:
        target = _target(session, caller, ref)
        return acts.my_requests(access.who_of(caller), target.obj)


def withdraw(caller: Caller, ref: str, request_id: str) -> dict[str, Any]:
    with session_scope() as session:
        target = _target(session, caller, ref)
        return _library_errors(lambda: acts.withdraw_request(access.who_of(caller), target.obj, request_id))


def remind(caller: Caller, ref: str, request_id: str) -> dict[str, Any]:
    with session_scope() as session:
        target = _target(session, caller, ref)
        out = _library_errors(lambda: acts.remind_request(access.who_of(caller), target.obj, request_id))
    _tell(target.obj.owner, f"{caller.principal} is still waiting for access to “{target.name}”", "",
          f"access-remind:{request_id}:{out['reminded_at']}")
    return out


def requests(caller: Caller, ref: str) -> list[dict[str, Any]]:
    """The open requests, for whoever may answer them (the share dialog's banner)."""
    with session_scope() as session:
        target = _target(session, caller, ref)
        return _library_errors(lambda: acts.requests_for(access.who_of(caller), target.obj))


def answer(caller: Caller, ref: str, request_id: str, *, approve: bool, role: object = None,
           reason: str = "") -> dict[str, Any]:
    """Share (the role chosen, maybe lower than asked) or Decline with a reason (SHR-R6.2)."""
    granted = _role(role) if role else None
    with session_scope() as session:
        target = _target(session, caller, ref)
        if approve:
            # The clearance check holds for a share made by answering a request too.
            from axiom.extensions.builtins.authz.db_models import AccessRequest
            from axiom.governance.access import store

            with store.session_scope() as s:
                row = s.get(AccessRequest, request_id)
                requester = row.requester if row is not None else ""
            if requester:
                _check_clearance(target, requester)
        answered, change = _library_errors(lambda: acts.answer_request(
            access.who_of(caller), target.obj, request_id, approve=approve, role=granted, reason=reason,
            surface=caller.surface))
        requester = answered["requester"]
        if approve:
            words = f"{caller.principal} gave {requester} {answered['granted_role']} access, as asked"
            _event(session, caller, target, "share", words, **(change.detail if change else {}),
                   receipt_id=change.receipt_id if change else None)
        else:
            words = f"{caller.principal} declined {requester}'s request"
            _event(session, caller, target, "decline", words, reason=answered["reason"],
                   receipt_id=change.receipt_id if change else None)
        session.commit()
    if approve:
        _tell(requester, f"{caller.principal} gave you {answered['granted_role']} access to “{target.name}”",
              "", f"access-approved:{request_id}")
    else:
        _tell(requester, f"{caller.principal} declined your request", answered["reason"] or "",
              f"access-declined:{request_id}")
    return {**answered, "words": words}


__all__ = [
    "FOLDER_PREFIX",
    "answer",
    "general",
    "my_request",
    "parse_general",
    "remind",
    "request",
    "requests",
    "set_notifier",
    "settings",
    "share",
    "transfer",
    "unshare",
    "who_has_access",
    "withdraw",
]
