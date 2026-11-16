# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

""""Needs you": one person's queue, the same everywhere (MVP-21).

One name on every surface: the attention bar's chip, the control room's
"Waiting for you" and chat's "what needs me" all read this function. It is a
read over stores that already exist, never a store of its own:

- **Due**: the person's unread inbox rows (alerts, requests, invitations to
  approve), then what the agenda says is due for them in the next 14 days
  (key and credential expiry, obligations; ``schedule.due_soon``).
- **Held drafts**: calls the person made that are held for an approval
  (the approval queue's pending actions they asked for).

- **Feedback** (LOOP-B5): one ask per cause that people or agents reported bad, for its
  owner only, with the rollback already chosen; its notification is not listed beside it.

- **To sign** and **Co-sign** (logbook unit X1): the person's open log drafts, and the entries
  waiting for a co-signature a role they hold may give (never their own), each linking to the Log
  page with no action: signing happens at the card, never from a queue.

The queue is person-scoped: the
person is the caller's own principal, never a parameter, so nobody reads
another person's queue (MVP-21-A1). ``count`` is ``len(items)`` by
construction, so the chip's number and the list cannot disagree.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: How far ahead "due soon" looks.
DUE_SOON_DAYS = 14

SECTIONS: tuple[tuple[str, str], ...] = (
    ("due", "Due"),
    ("held", "Held drafts"),
    ("sign", "To sign"),
    ("cosign", "Co-sign"),
)


def _inbox_items(person: str, inbox_store: Any, limit: int) -> list[dict[str, Any]]:
    from .inbox import InboxQuery

    rows = inbox_store.query(InboxQuery(recipient=person, unread_only=True, limit=limit))
    out = []
    for r in rows:
        if getattr(r, "recipient", person) != person:
            continue  # a store that ignored the filter does not leak another person's row
        out.append({
            "id": f"inbox:{r.id}",
            "section": "due",
            "kind": "notice",
            "title": r.summary,
            "source": "notifications",
            "due": None,
            "priority": r.priority,
            "href": getattr(r, "link", "") or None,
            "action": {"label": "Acknowledge", "skill": "notifications.ack", "params": {"id": r.id}},
            "receipt_id": getattr(r, "receipt_id", None),
            "created_at": r.created_at.isoformat() if getattr(r, "created_at", None) else None,
        })
    return out


def _due_items(person: str, due_provider: Callable[[str, int], Iterable[dict]] | None) -> list[dict[str, Any]]:
    if due_provider is None:
        try:
            from axiom.extensions.builtins.schedule.items import due_soon

            def due_provider(p: str, days: int) -> Iterable[dict]:
                return due_soon(p, days=days)
        except Exception:  # noqa: BLE001 - no agenda yet: the queue still answers
            return []
    try:
        items = list(due_provider(person, DUE_SOON_DAYS))
    except Exception as exc:  # noqa: BLE001 - one failing source never empties the queue
        log.warning("needs-you: the agenda could not be read: %s", exc)
        return []
    out = []
    for it in items:
        link = it.get("owner_link") or it.get("href")
        if isinstance(link, dict):  # the agenda's {"cli", "web"}: the page wants the web one
            link = link.get("web") or None
        out.append({
            "id": f"agenda:{it.get('id')}",
            "section": "due",
            "kind": it.get("kind") or "due",
            "title": it.get("title") or "",
            "source": it.get("source") or "schedule",
            "due": it.get("start") or it.get("due"),
            "date_only": bool(it.get("date_only")),
            "priority": "normal",
            "href": link,
            "timezone": it.get("timezone"),
            "action": None,
            "created_at": None,
        })
    return out


def _held_items(person: str, approvals_path: Path | None) -> list[dict[str, Any]]:
    try:
        from axiom.infra.orchestrator.actions import ActionStatus
        from axiom.infra.orchestrator.approval_store import FileActionStore, default_approval_path

        path = approvals_path or default_approval_path()
        if not Path(path).exists():
            return []
        actions = FileActionStore(path).all()
    except Exception as exc:  # noqa: BLE001 - an unreadable queue lists nothing held
        log.warning("needs-you: the approval queue could not be read: %s", exc)
        return []
    out = []
    for a in actions:
        if a.status != ActionStatus.PENDING or a.requested_by != person:
            continue
        out.append({
            "id": f"held:{a.action_id}",
            "section": "held",
            "kind": "held",
            "title": f"{a.name} is held for approval",
            "source": "approvals",
            "due": None,
            "priority": "normal",
            "href": None,
            "action": None,
            "reason": a.reason,
            "created_at": a.created_at,
        })
    return out


def _attest_items(person: str) -> list[dict[str, Any]]:
    try:
        from axiom.extensions.builtins.attest.queue import for_person
    except Exception:  # noqa: BLE001 - no logs on this node: the queue still answers
        return []
    try:
        return list(for_person(person))
    except Exception as exc:  # noqa: BLE001 - one failing source never empties the queue
        log.warning("needs-you: the logs could not be read: %s", exc)
        return []


def _feedback_items(person: str) -> list[dict[str, Any]]:
    try:
        from axiom.extensions.builtins.feedback.queue import for_person
    except Exception:  # noqa: BLE001 - no feedback on this node: the queue still answers
        return []
    try:
        return list(for_person(person))
    except Exception as exc:  # noqa: BLE001 - one failing source never empties the queue
        log.warning("needs-you: feedback could not be read: %s", exc)
        return []


def most_frequent_due(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The due task of the most frequent kind, soonest first: shown one click from landing."""
    due = [i for i in items if i["section"] == "due"]
    if not due:
        return None
    counts = Counter(i["kind"] for i in due)
    first: dict[str, int] = {}
    for n, i in enumerate(due):
        first.setdefault(i["kind"], n)
    top_kind = max(counts, key=lambda k: (counts[k], -first[k]))
    same = [i for i in due if i["kind"] == top_kind]
    return sorted(same, key=lambda i: (i.get("due") is None, str(i.get("due") or "")))[0]


def needs_you(
    person: str,
    *,
    inbox_store: Any = None,
    approvals_path: Path | None = None,
    due_provider: Callable[[str, int], Iterable[dict]] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """The person's queue: sections, the flat list, its count, and the top due task."""
    if inbox_store is None:
        from .inbox_db import default_inbox_store

        inbox_store = default_inbox_store()
    feedback = _feedback_items(person)
    # An ask's own notification is the ask, not a second item (ADR-253 D5).
    notices = {r for item in feedback for r in item.get("notices") or ()}
    try:
        inbox = [i for i in _inbox_items(person, inbox_store, limit) if i.get("receipt_id") not in notices]
    except Exception as exc:  # noqa: BLE001 - one failing source never empties the queue
        log.warning("needs-you: the inbox could not be read: %s", exc)
        inbox = []
    due = sorted(_due_items(person, due_provider), key=lambda i: str(i.get("due") or ""))
    items = (feedback + inbox + due + _held_items(person, approvals_path) + _attest_items(person))[:limit]
    sections = [
        {"id": sid, "label": label, "items": [i for i in items if i["section"] == sid]}
        for sid, label in SECTIONS
    ]
    return {
        "person": person,
        "name": "Needs you",
        "count": len(items),
        "items": items,
        "sections": sections,
        "top": most_frequent_due(items),
    }


__all__ = ["DUE_SOON_DAYS", "SECTIONS", "most_frequent_due", "needs_you"]
