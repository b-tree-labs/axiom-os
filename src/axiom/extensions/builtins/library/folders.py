# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Folder verbs, each through the one decision point (prd-sharing-and-access SHR-R4; #1952).

- **Create** needs only being at the site; the new folder is the maker's and Restricted (SHR-R4.6).
- **Rename or move** a person's folder needs Editor on it; moving it into
  another person's folder needs Editor there too. A Data Book scheme category is
  renamed, moved or hidden by a **Book manager** only (SHR-R4.4).
- **Delete** a person's folder needs Owner or Editor, and takes what it holds:
  the folder, its subfolders and every item in them go to Deleted files as one
  act with one undo. Each item is checked on its own: one held for review, under
  a legal hold, or one the person may not delete stays, and the answer says how
  many and why; a folder that keeps such an item keeps only those (SHR-R4.5).
"""

from __future__ import annotations

import uuid
from typing import Any

from axiom.extensions.builtins.library import access
from axiom.extensions.builtins.library.scope import Caller
from axiom.extensions.builtins.library.store import session_scope


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _user_folder(session: Any, caller: Caller, path: str) -> tuple[Any, dict[str, Any]] | None:
    folders = access.user_folders(session, caller.site)
    row = folders.get(path)
    return (row, folders) if row is not None else None


def create(caller: Caller, label: str, parent: str = "") -> dict[str, Any]:
    from axiom.extensions.builtins.library.book import service as book

    with session_scope() as session:
        parent_path = (parent or "").strip("/")
        found = _user_folder(session, caller, parent_path) if parent_path else None
        if found is not None:
            row, folders = found
            access.require(session, caller, "library.folder.file_into", access.folder_object(row, folders))
        out = book.create_folder(session, caller.site, caller.principal, label, parent_path)
        session.commit()
        return out


def change(caller: Caller, path: str, *, label: str | None, parent: str | None) -> dict[str, Any]:
    from axiom.extensions.builtins.library.book import service as book

    path = (path or "").strip("/")
    with session_scope() as session:
        found = _user_folder(session, caller, path)
        if found is not None:
            row, folders = found
            access.require(session, caller, "library.folder.rename", access.folder_object(row, folders))
        else:
            if path not in book.active_scheme().by_path:
                raise LookupError(f"no folder {path!r} in this Book")
            access.require(session, caller, "library.book.category", access.category_object(caller.site, path))
        if parent:
            target = _user_folder(session, caller, parent.strip("/"))
            if target is not None:
                trow, tfolders = target
                access.require(session, caller, "library.folder.file_into", access.folder_object(trow, tfolders))
        out = book.change_folder(session, caller.site, caller.principal, path, label=label, parent=parent)
        session.commit()
        return out


def _stays_because(session: Any, caller: Caller, item: Any) -> str | None:
    from axiom.extensions.builtins.library import labels
    from axiom.extensions.builtins.library.service import Forbidden, NotFound
    from axiom.governance.retention import RetentionClass

    if (item.ec or {}).get("verdict") == "held":
        return "held for export-control review"
    if labels.class_of(item) is RetentionClass.LEGAL_HOLD:
        return "under a legal hold"
    try:
        access.require_item(session, caller, "library.delete", item)
    except (Forbidden, NotFound):
        return "not yours to delete"
    return None


def delete(caller: Caller, path: str) -> dict[str, Any]:
    """The folder and what it holds to Deleted files, as one act (SHR-R4.5)."""
    from axiom.extensions.builtins.library import service
    from axiom.extensions.builtins.library.db_models import LibraryItem

    path = (path or "").strip("/")
    act = str(uuid.uuid4())
    with session_scope() as session:
        found = _user_folder(session, caller, path)
        if found is None:
            raise LookupError(
                f"no folder of yours at {path!r} (a Data Book category is renamed or moved, never deleted)"
            )
        row, folders = found
        access.require(session, caller, "library.folder.delete", access.folder_object(row, folders))
        under = [f for p, f in folders.items() if p == path or p.startswith(path + "/")]
        items = (
            session.query(LibraryItem)
            .filter(LibraryItem.site == caller.site, LibraryItem.deleted_at.is_(None),
                    (LibraryItem.folder == path) | (LibraryItem.folder.like(path + "/%")))
            .all()
        )
        stayed: dict[str, int] = {}
        kept_in: set[str] = set()
        gone = 0
        for item in items:
            why = _stays_because(session, caller, item)
            if why is not None:
                stayed[why] = stayed.get(why, 0) + 1
                kept_in.add(item.folder or "")
                continue
            service._tombstone_in(session, item, caller, act=act)  # noqa: SLF001
            gone += 1
        # A folder that keeps such an item keeps only those, and its parents stay so nothing is orphaned.
        keep = {p for k in kept_in for p in folders if k == p or k.startswith(p + "/")}
        removed = 0
        for f in under:
            if f.path in keep:
                continue
            f.deleted_at = _now()
            f.deleted_with = act
            removed += 1
        session.commit()
    stayed_list = [{"count": n, "why": why} for why, n in sorted(stayed.items())]
    line = f"Deleted {gone} file{'s' if gone != 1 else ''}"
    if stayed_list:
        line += "; " + ", ".join(f"{s['count']} stayed ({s['why']})" for s in stayed_list)
    return {"path": path, "deleted": removed > 0, "deleted_items": gone, "deleted_folders": removed,
            "stayed": stayed_list, "undo": act, "message": line + "."}


def restore(caller: Caller, act: str) -> dict[str, Any]:
    """Undo one folder delete: its folders and every item it took come back."""
    from axiom.extensions.builtins.library import service
    from axiom.extensions.builtins.library.db_models import LibraryFolder, LibraryItem

    act = (act or "").strip()
    if not act:
        raise ValueError("say which delete to undo")
    with session_scope() as session:
        folders = (
            session.query(LibraryFolder)
            .filter(LibraryFolder.site == caller.site, LibraryFolder.deleted_with == act)
            .all()
        )
        items = (
            session.query(LibraryItem)
            .filter(LibraryItem.site == caller.site, LibraryItem.deleted_with == act)
            .all()
        )
        if not folders and not items:
            raise LookupError("nothing to undo for that delete")
        live = access.user_folders(session, caller.site)
        for f in folders:
            # The person who deleted the folder may bring it back: they had Owner or Editor on it.
            access.require(session, caller, "library.folder.delete", access.folder_object(f, {**live, f.path: f}))
        for f in folders:
            f.deleted_at, f.deleted_with = None, None
        restored = 0
        for item in items:
            if item.deleted_at is None:
                continue
            access.require_item(session, caller, "library.restore", item)
            service._restore_in(session, item, caller)  # noqa: SLF001
            restored += 1
        session.commit()
    return {"restored_items": restored, "restored_folders": len(folders)}


__all__ = ["change", "create", "delete", "restore"]
