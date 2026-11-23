# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""KEEP owns access expiry for the Library (prd-sharing-and-access SHR-R7.1, SHR-A6; ADR-209 D8a).

An hourly tick (the ``library.access_expiry`` schedule):

- **A week before**, one reminder to the person who gave the access, naming who
  loses it, on what, and when; sent once (``reminded_at``).
- **On the day**, the entry ends (``revoked_reason = expired``) and the end is a
  line in the item's or folder's activity, as KEEP did it.

A failed notice is logged and retried on the next tick (the reminder is marked
only once it is sent); ending an entry never waits on a notice.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from axiom.governance.access import store

log = logging.getLogger(__name__)

KEEP = "@keep"
REMIND_BEFORE = timedelta(days=7)


def _names(session: Any, grants: list[store.Grant]) -> dict[str, str]:
    from axiom.extensions.builtins.library.db_models import LibraryFolder, LibraryItem

    out: dict[str, str] = {}
    docs = [g.object_id for g in grants if g.kind == "document"]
    folders = [g.object_id for g in grants if g.kind == "folder"]
    if docs:
        for r in session.query(LibraryItem).filter(LibraryItem.id.in_(docs)).all():
            out[r.id] = r.title or r.file_name
    if folders:
        for r in session.query(LibraryFolder).filter(LibraryFolder.id.in_(folders)).all():
            out[r.id] = r.label
    return out


def _event(session: Any, g: store.Grant, words: str) -> None:
    from axiom.extensions.builtins.library.db_models import LibraryEvent

    session.add(LibraryEvent(item_id=g.object_id, site=g.site, principal=KEEP, action="expire", surface="schedule",
                             detail={"words": words, "subject": g.subject, "role": g.role.value, "grant_id": g.id},
                             at=store.now()))


def tick(params: dict[str, Any] | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    """Remind once a week ahead, and end what is due. Returns the counts."""
    from axiom.extensions.builtins.library.sharing import _tell
    from axiom.extensions.builtins.library.store import session_scope

    when = now or store.now()
    reminded = ended = 0
    with store.session_scope() as s:
        due = [g for g in store.due_reminders(s, within=REMIND_BEFORE, at=when) if g.kind in ("document", "folder")]
        expired = [g for g in store.expire_due(s, at=when) if g.kind in ("document", "folder")]
        with session_scope() as lib:
            names = _names(lib, due + expired)
            for g in due:
                if g.expires_at is None or g.expires_at <= when:
                    continue
                what = names.get(g.object_id, "a file")
                day = g.expires_at.date().isoformat()
                if _tell(g.granted_by, f"{g.subject}'s access to “{what}” ends on {day}",
                         "Share it again before then if they still need it.", f"access-expiry:{g.id}"):
                    store.mark_reminded(s, g.id, at=when)
                    reminded += 1
            for g in expired:
                _event(lib, g, f"{g.subject}'s access ended on its day")
                ended += 1
            lib.commit()
        s.commit()
    return {"reminded": reminded, "ended": ended}


__all__ = ["KEEP", "REMIND_BEFORE", "tick"]
