# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Same name on upload: Replace (a new version), Keep both, or Cancel (spec-upload-pipeline §2).

A client opts in with ``on_conflict``:

- ``ask``: a live file the caller can see has this name (ignoring case) in the
  same place and other bytes, so the upload is refused with :class:`NameConflict`
  (HTTP 409) naming it. Identical bytes are not a conflict: the duplicate check
  in ``stored`` files them once.
- ``keep_both``: the new file is filed as ``name (1).ext``, the first number free.
- ``replace``: the new bytes become the next version of the existing file, which
  only its owner may do.

"The same place" is the folder the upload goes into when one is given (the
folder it was filed in or asked for), and otherwise the whole Library, which
files uploads into its Book by itself. Cancel is the client's: it sends nothing.
Without ``on_conflict`` nothing here runs, so the CLI, skills and older clients
behave as before.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from axiom.extensions.builtins.library.db_models import LibraryItem
from axiom.extensions.builtins.library.scope import Caller, can_change, can_read

ANSWERS = ("ask", "replace", "keep_both")
MAX_NUMBER = 999


class NameConflict(Exception):  # noqa: N818 - a protocol answer, like OffsetMismatch
    """The name is taken; ``payload`` says by what, and what the caller may do."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(f"{payload['file_name']!r} is already in the library")
        self.payload = payload


def clean_answer(raw: Any) -> str:
    value = str(raw or "").strip().lower().replace("-", "_")
    if value and value not in ANSWERS:
        raise ValueError(f"on_conflict must be one of {', '.join(ANSWERS)}")
    return value


def _in_place(item: LibraryItem, folder: str) -> bool:
    if not folder:
        return True
    requested = str((item.placement or {}).get("requested_folder") or "")
    return folder in (item.folder or "", requested)


def _taken(session: Any, caller: Caller, folder: str) -> list[LibraryItem]:
    rows = (
        session.query(LibraryItem)
        .filter(LibraryItem.site == caller.site, LibraryItem.deleted_at.is_(None))
        .all()
    )
    from axiom.extensions.builtins.library import access

    placed = [r for r in rows if _in_place(r, folder)]
    seen = access.readable_ids(session, caller, placed)
    return [r for r in placed if r.id in seen]


def find(session: Any, caller: Caller, name: str, *, folder: str = "") -> LibraryItem | None:
    """The newest live file the caller can see with this name in this place, or ``None``."""
    want = name.casefold()
    same = [r for r in _taken(session, caller, folder) if (r.file_name or "").casefold() == want]
    if not same:
        return None
    return max(same, key=lambda r: (r.updated_at or r.created_at) if (r.updated_at or r.created_at) else 0)


def keep_both_name(session: Any, caller: Caller, name: str, *, folder: str = "") -> str:
    """``name (n).ext`` with the first ``n`` no file in this place has."""
    taken = {(r.file_name or "").casefold() for r in _taken(session, caller, folder)}
    path = PurePosixPath(name)
    suffix = "".join(path.suffixes[-1:])
    stem = name[: len(name) - len(suffix)] if suffix else name
    for n in range(1, MAX_NUMBER + 1):
        candidate = f"{stem} ({n}){suffix}"
        if candidate.casefold() not in taken:
            return candidate
    raise ValueError(f"{name!r} has {MAX_NUMBER} copies here already; rename it")


def payload(session: Any, caller: Caller, name: str, existing: LibraryItem, *, folder: str = "") -> dict[str, Any]:
    created = existing.created_at.isoformat() if existing.created_at else None
    return {
        "code": "name_conflict",
        "file_name": name,
        "existing": {
            "id": existing.id,
            "file_name": existing.file_name,
            "title": existing.title,
            "size": existing.size,
            "owner": existing.owner,
            "folder": existing.folder or "",
            "created_at": created,
        },
        "can_replace": can_change(caller, existing),
        "keep_both_name": keep_both_name(session, caller, name, folder=folder),
    }


def resolve(
    session: Any,
    caller: Caller,
    name: str,
    *,
    answer: str,
    sha256: str = "",
    folder: str = "",
    replace_item_id: str = "",
) -> tuple[str, str]:
    """Apply the person's answer before anything is staged: ``(file name to file under, item to replace or "")``.

    Raises :class:`NameConflict` for ``ask`` and ``PermissionError`` when the
    file to replace is not the caller's.
    """
    from axiom.extensions.builtins.library.service import Forbidden, NotFound

    if not answer:
        return name, ""
    folder = folder or ""
    if answer == "replace" and replace_item_id:
        target = session.get(LibraryItem, replace_item_id)
        if target is None or target.deleted_at is not None or not can_read(caller, target):
            raise NotFound(replace_item_id)
    else:
        target = find(session, caller, name, folder=folder)
    if target is None:
        return name, ""
    if sha256 and target.sha256 == sha256.lower():
        return name, ""  # identical bytes: the duplicate check files it once
    if answer == "ask":
        raise NameConflict(payload(session, caller, name, target, folder=folder))
    if answer == "keep_both":
        return keep_both_name(session, caller, name, folder=folder), ""
    if not can_change(caller, target):
        raise Forbidden("replacing a file needs edit access to it; keep both instead")
    return name, target.id


__all__ = ["ANSWERS", "NameConflict", "clean_answer", "find", "keep_both_name", "payload", "resolve"]
