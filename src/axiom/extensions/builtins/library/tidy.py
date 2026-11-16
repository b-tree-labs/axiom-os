# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tidy up: propose a reorganisation of a person's files, then apply only what they accept.

Ported from the earlier product's Library, where "Tidy up" was one of the four
additions it made to the familiar file browser it followed. The engine never
changes anything when it proposes; the person reviews each proposal and apply
changes exactly the accepted items, through the same calls a person's own
move and delete use, so every check, event and record is the usual one.

Two kinds of proposal, deterministic (the same Library proposes the same list,
sorted by kind then item id), for the caller's own live files only:

- ``delete_duplicate``: files with the same bytes (the same SHA-256). The
  earliest is kept; each later copy is proposed for Deleted files, where it can
  be restored.
- ``move``: the Book would file it elsewhere now (a person moved similar files,
  or made a folder that fits) and is confident. A file a person placed is never
  proposed: their move stands. A move inferred from someone's move or a folder's
  resemblance is proposed below high confidence, so the person ticks it themselves.

An accepted move is recorded as the person's move, so the Book learns from it
(the original's training signal). Apply re-checks each item when it runs: a
proposal that no longer holds is skipped with the reason, never forced.
"""

from __future__ import annotations

from typing import Any

from axiom.extensions.builtins.library.db_models import LibraryItem
from axiom.extensions.builtins.library.scope import Caller, can_change
from axiom.extensions.builtins.library.store import session_scope

ACTIONS = ("delete_duplicate", "move")
#: How many files one proposal run reads; a larger Library is tidied in parts (``item_ids``).
MAX_CONSIDERED = 500
MAX_APPLY = 500
#: A move the Book only inferred (from a person's move, or a folder's resemblance) is a guess,
#: however sure the Book is when it files a new upload: below high, so the review starts it unticked.
INFERRED_CEILING = 0.7
INFERRED = ("correction", "user-folder")


def _iso(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


def _mine(session: Any, caller: Caller, item_ids: list[str] | None) -> list[LibraryItem]:
    q = session.query(LibraryItem).filter(
        LibraryItem.site == caller.site, LibraryItem.owner == caller.principal, LibraryItem.deleted_at.is_(None)
    )
    if item_ids:
        q = q.filter(LibraryItem.id.in_([str(i) for i in item_ids][:MAX_CONSIDERED]))
    return q.order_by(LibraryItem.created_at, LibraryItem.id).limit(MAX_CONSIDERED + 1).all()


def _labels_for(session: Any, site: str, path: str) -> list[str]:
    from axiom.extensions.builtins.library.book import service as book

    return book._user_labels(session, site, path, book.active_scheme()) if path else []


def _duplicates(session: Any, items: list[LibraryItem]) -> list[dict[str, Any]]:
    groups: dict[str, list[LibraryItem]] = {}
    for item in items:
        if item.sha256:
            groups.setdefault(item.sha256, []).append(item)
    out = []
    for group in groups.values():
        if len(group) < 2:
            continue
        keep, *rest = sorted(group, key=lambda i: (i.created_at is None, _iso(i.created_at) or "", i.id))
        for copy in rest:
            out.append({
                "item_id": copy.id, "file_name": copy.file_name, "title": copy.title, "action": "delete_duplicate",
                "from_folder": copy.folder or "", "from_labels": _labels_for(session, copy.site, copy.folder or ""),
                "duplicate_of": {"id": keep.id, "file_name": keep.file_name, "title": keep.title,
                                 "folder": keep.folder or ""},
                "because": f"it has the same contents as {keep.title or keep.file_name!r}, which is kept",
                "confidence": 1.0,
            })
    return out


def _refiles(session: Any, items: list[LibraryItem], taken: set[str]) -> list[dict[str, Any]]:
    from axiom.extensions.builtins.library import pipeline
    from axiom.extensions.builtins.library.book import service as book

    scheme = book.active_scheme()
    out = []
    for item in items:
        placement = item.placement or {}
        if item.id in taken or placement.get("source") == "manual":
            continue  # a person's placement stands; a duplicate is handled as one
        done = pipeline._current_results(item.id, exclude=set())
        recognised = pipeline._current_recognised(item.id, exclude=set())
        placed, _ = book.file_item(session, item.site, pipeline._signals(item, done, recognised), owner=item.owner)
        if placed.needs_look or placed.path in ("", scheme.unsorted) or placed.path == (item.folder or ""):
            continue
        out.append({
            "item_id": item.id, "file_name": item.file_name, "title": item.title, "action": "move",
            "from_folder": item.folder or "", "from_labels": _labels_for(session, item.site, item.folder or ""),
            "to_folder": placed.path, "to_labels": list(placed.labels),
            "because": placed.because,
            "confidence": round(min(float(placed.confidence), INFERRED_CEILING)
                                if placed.source in INFERRED else float(placed.confidence), 3),
        })
    return out


def propose(caller: Caller, *, item_ids: list[str] | None = None) -> dict[str, Any]:
    """What Tidy would change, and why. Changes nothing."""
    with session_scope() as session:
        items = _mine(session, caller, item_ids)
        capped = len(items) > MAX_CONSIDERED
        items = items[:MAX_CONSIDERED]
        dups = _duplicates(session, items)
        moves = _refiles(session, items, {d["item_id"] for d in dups})
        session.rollback()  # filing is computed, never kept, here
    proposals = sorted(dups + moves, key=lambda p: (ACTIONS.index(p["action"]), p["item_id"]))
    return {"proposals": proposals, "considered": len(items), "capped": capped}


def _apply_one(caller: Caller, want: dict[str, Any]) -> tuple[bool, str]:
    from axiom.extensions.builtins.library import service

    item_id, action = str(want.get("item_id") or ""), str(want.get("action") or "")
    if action not in ACTIONS:
        return False, f"no Tidy action {action!r}"
    with session_scope() as session:
        item = session.get(LibraryItem, item_id)
        if item is None or item.deleted_at is not None or not can_change(caller, item):
            return False, "not one of your files"
        if action == "delete_duplicate":
            others = (
                session.query(LibraryItem)
                .filter(LibraryItem.site == item.site, LibraryItem.sha256 == item.sha256, LibraryItem.id != item.id,
                        LibraryItem.deleted_at.is_(None), LibraryItem.owner == caller.principal)
                .count()
            )
            if not others:
                return False, "no other copy is left; it is kept"
    if action == "delete_duplicate":
        service.delete_item(caller, item_id)
        return True, "moved to Deleted files"
    to = str(want.get("to_folder") or "").strip("/")
    if not to:
        return False, "no folder to move it to"
    moved = service.move_item(caller, item_id, to)
    return True, f"moved to {' / '.join((moved.get('placement') or {}).get('labels') or [to])}"


def apply(caller: Caller, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply the accepted proposals, each re-checked now; anything that no longer holds is skipped, with why."""
    applied, skipped = [], []
    for want in list(items or [])[:MAX_APPLY]:
        try:
            ok, note = _apply_one(caller, want)
        except (LookupError, PermissionError, ValueError) as exc:
            ok, note = False, str(exc)
        entry = {"item_id": str(want.get("item_id") or ""), "action": str(want.get("action") or ""), "detail": note}
        (applied if ok else skipped).append(entry if ok else {**entry, "reason": note})
    return {"applied": applied, "skipped": skipped}


__all__ = ["ACTIONS", "MAX_CONSIDERED", "apply", "propose"]
