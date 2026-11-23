# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Who runs a site's flags, and the ask a waiting change raises (ADR-253, SW-A16, SW-A18).

* :func:`site_admins`: the people the directory says hold ``site-admin`` here (ADR-103: their
  groups mapped to roles by the deployment's role map), in a stable order. Explain names the
  first as the person to ask; a waiting change asks all of them but its author.
* :func:`raise_for_change` / :func:`resolve_for_change`: a change that waits for a approval
  is an ask in each other site admin's Needs you, linking to the change. Approving, withdrawing
  or applying it resolves the ask everywhere. The ask rides the inbox Needs you already reads
  (an unread row with a link); nothing here keeps a queue of its own.

Every function is best-effort: an unreadable directory or inbox never fails the change.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

#: The page a change's ask opens.
CHANGE_HREF = "#/settings/flags/change/{id}"

_inbox_store: Any = None


def set_inbox_store(store: Any) -> None:
    """Pin the inbox asks are written to (tests), or ``None`` for the node's own."""
    global _inbox_store
    _inbox_store = store


def _inbox() -> Any:
    if _inbox_store is not None:
        return _inbox_store
    from axiom.extensions.builtins.notifications.inbox_db import default_inbox_store

    return default_inbox_store()


def _handle(subject: str) -> str:
    subject = str(subject or "").strip()
    return subject if subject.startswith("@") or not subject else f"@{subject}"


def site_admins() -> list[dict[str, str]]:
    """``[{principal, name}]`` for everyone the directory maps to ``site-admin``, sorted; ``[]``
    when there is no directory, it cannot list a group, or nobody holds the role."""
    try:
        from axiom.extensions.builtins.directory import GroupRef
        from axiom.extensions.builtins.directory.config import load_directory_config
        from axiom.governance.roles import SITE_ADMIN

        cfg = load_directory_config()
        if cfg.provider is None:
            return []
        groups = {str(rule.get("group") or "") for rule in cfg.role_map.rules}
        groups |= {ref.id for ref in cfg.sync_groups}
        found: dict[str, str] = {}
        for gid in sorted(g for g in groups if g and not any(ch in g for ch in "*?[")):
            if SITE_ADMIN not in cfg.role_map.roles_for([gid]):
                continue
            for member in cfg.provider.members_of(GroupRef(id=gid, provider=cfg.provider_name)):
                principal = _handle(member.subject)
                if principal:
                    found.setdefault(principal, str(member.display or principal))
        return [{"principal": p, "name": found[p]} for p in sorted(found)]
    except Exception as exc:  # noqa: BLE001 - no directory names nobody
        log.debug("site admins could not be read from the directory: %s", exc)
        return []


def _name(handle: str) -> str:
    from .approvals import _name as name

    return name(handle)


def _recipients(author: str) -> list[str]:
    people = [a["principal"] for a in site_admins() if _name(a["principal"]) != _name(author)]
    if people:
        return people
    try:  # no directory: the role's holder from the node's settings, if somebody else
        from axiom.extensions.builtins.notifications.preferences import resolve_role

        holder = resolve_role("@site-admin")
    except Exception:  # noqa: BLE001
        return []
    return [holder] if holder.startswith("@") and holder != "@site-admin" and _name(holder) != _name(author) else []


def ask_text(change: dict[str, Any]) -> str:
    """The one sentence a site admin reads in Needs you."""
    return f"{change['author']} asks you to approve a change to {change['subject']}."


def raise_for_change(change: dict[str, Any]) -> list[str]:
    """Ask every other site admin to approve ``change``; the recipients asked."""
    store = _inbox()
    if not hasattr(store, "write_alert"):
        return []
    href = CHANGE_HREF.format(id=change["id"])
    asked = []
    for recipient in _recipients(change["author"]):
        try:
            store.write_alert(recipient=recipient, summary=ask_text(change), priority="normal",
                              classification="internal", actor=change["author"], link=href,
                              dedup_key=f"flags-change:{change['id']}:{recipient}")
            asked.append(recipient)
        except Exception as exc:  # noqa: BLE001 - an inbox that cannot be written never holds a change
            log.warning("could not ask %s to approve change %s: %s", recipient, change["id"], exc)
    return asked


def resolve_for_change(change_id: str, author: str = "") -> int:
    """Resolve the ask for ``change_id`` in every recipient's Needs you; how many were open."""
    store = _inbox()
    href = CHANGE_HREF.format(id=change_id)
    try:
        from axiom.extensions.builtins.notifications.inbox import InboxQuery

        rows = []
        for recipient in _recipients(author):
            rows += [r for r in store.query(InboxQuery(recipient=recipient, unread_only=True, limit=200))
                     if getattr(r, "link", "") == href]
        for row in rows:
            store.mark_read(row_id=row.id)
        return len(rows)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not resolve the ask for change %s: %s", change_id, exc)
        return 0


__all__ = ["CHANGE_HREF", "ask_text", "raise_for_change", "resolve_for_change", "set_inbox_store", "site_admins"]
