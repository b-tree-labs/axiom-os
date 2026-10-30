# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The Data Book at a site: the active scheme, its folder tree, user folders, moves (ADR-213 D11).

A folder's **path is its identity**: items are filed by path, a rename changes
only its label, and a move changes only where it is shown (``shown_under``), so
no item is ever re-filed because somebody tidied the tree. User folders are
site-wide (the Book is the site's), made and changed by anyone who can file at
the site, and recorded in the library's events like every other change.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.library import contrib
from axiom.extensions.builtins.library.book.place import (
    Correction,
    Placement,
    Signals,
    UserFolder,
    place,
)
from axiom.extensions.builtins.library.book.scheme import (
    UNSORTED_LABEL,
    BookScheme,
    SchemeError,
    load_scheme_file,
    scheme_from_dict,
)

log = logging.getLogger(__name__)

BOOK_ENV = "AXIOM_LIBRARY_BOOK"
GENERIC = "data"
USER_ROOT = "my-folders"
MAX_CORRECTIONS = 500
MEMBERS_PER_FOLDER = 50

_lock = threading.Lock()
_schemes: dict[str, BookScheme] | None = None


def _reset() -> None:
    global _schemes
    with _lock:
        _schemes = None


contrib.on_change(_reset)


def _load() -> dict[str, BookScheme]:
    out: dict[str, BookScheme] = {}
    out[GENERIC] = load_scheme_file(Path(__file__).with_name("data-book.toml"), source="library")
    for c in contrib.contributions("book_schemes"):
        try:
            if c.data.get("file"):
                if c.root is None:
                    raise SchemeError("a scheme file needs the manifest's directory")
                s = load_scheme_file(c.root / str(c.data["file"]), source=c.source)
            else:
                s = scheme_from_dict(c.data, source=c.source)
        except (SchemeError, OSError, KeyError, ValueError) as exc:
            log.warning("library: Data Book scheme from %s refused: %s", c.source, exc)
            continue
        out[s.name] = s
    return out


def schemes() -> dict[str, BookScheme]:
    global _schemes
    with _lock:
        if _schemes is None:
            _schemes = _load()
        return dict(_schemes)


def active_scheme() -> BookScheme:
    """``AXIOM_LIBRARY_BOOK``, else the one product scheme installed, else the generic Data Book."""
    import os

    found = schemes()
    wanted = (os.environ.get(BOOK_ENV) or "").strip()
    if wanted and wanted in found:
        return found[wanted]
    products = sorted(n for n in found if n != GENERIC)
    if len(products) >= 1:
        if len(products) > 1:
            log.info("library: %d Data Book schemes installed (%s); set %s to choose", len(products), products, BOOK_ENV)
        return found[products[0]]
    return found[GENERIC]


def slug(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return s[:60] or "folder"


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- placement inputs


def user_folders(session: Any, site: str) -> list[UserFolder]:
    from axiom.extensions.builtins.library.db_models import LibraryFolder, LibraryItem

    rows = (
        session.query(LibraryFolder)
        .filter(LibraryFolder.site == site, LibraryFolder.kind == "user", LibraryFolder.deleted_at.is_(None))
        .all()
    )
    out = []
    for f in rows:
        members = (
            session.query(LibraryItem)
            .filter(LibraryItem.site == site, LibraryItem.folder == f.path, LibraryItem.deleted_at.is_(None))
            .order_by(LibraryItem.updated_at.desc())
            .limit(MEMBERS_PER_FOLDER)
            .all()
        )
        sigs = [set((m.placement or {}).get("signature") or []) for m in members]
        out.append(UserFolder(path=f.path, label=f.label, member_signatures=[s for s in sigs if s]))
    return out


def corrections(session: Any, site: str) -> list[Correction]:
    from axiom.extensions.builtins.library.db_models import LibraryCorrection

    rows = (
        session.query(LibraryCorrection)
        .filter(LibraryCorrection.site == site)
        .order_by(LibraryCorrection.id.desc())
        .limit(MAX_CORRECTIONS)
        .all()
    )
    return [Correction(signature=set(r.signature or []), to_path=r.to_path) for r in rows]


def file_item(session: Any, site: str, signals: Signals) -> tuple[Placement, BookScheme]:
    scheme = active_scheme()
    folders = user_folders(session, site)
    placed = place(signals, scheme, user_folders=folders, corrections=corrections(session, site))
    # A user folder's label, not its slug, is what the explanation names.
    by_path = {f.path: f.label for f in folders}
    if placed.path in by_path:
        placed.labels = [*_user_labels(session, site, placed.path, scheme)]
    return placed, scheme


def placement_json(placed: Placement, signals: Signals) -> dict[str, Any]:
    return {**placed.to_json(), "signature": sorted(signals.signature())}


# ----------------------------------------------------------------------- the tree


def _overrides(session: Any, site: str) -> dict[str, Any]:
    from axiom.extensions.builtins.library.db_models import LibraryFolder

    return {
        f.path: f
        for f in session.query(LibraryFolder)
        .filter(LibraryFolder.site == site, LibraryFolder.deleted_at.is_(None))
        .all()
    }


def _user_labels(session: Any, site: str, path: str, scheme: BookScheme) -> list[str]:
    folders = _overrides(session, site)
    parts = path.split("/")
    labels = []
    for i in range(len(parts)):
        p = "/".join(parts[: i + 1])
        labels.append(folders[p].label if p in folders else ("My folders" if p == USER_ROOT else scheme.label_for(p)))
    return labels


def tree(session: Any, site: str) -> dict[str, Any]:
    """Every folder with its label, kind, display parent and counts (filed here, needing a look)."""
    from sqlalchemy import func

    from axiom.extensions.builtins.library.db_models import LibraryItem

    scheme = active_scheme()
    folders = _overrides(session, site)
    counts: dict[str, int] = {}
    looks: dict[str, int] = {}
    rows = (
        session.query(LibraryItem.folder, func.count(LibraryItem.id))
        .filter(LibraryItem.site == site, LibraryItem.deleted_at.is_(None))
        .group_by(LibraryItem.folder)
        .all()
    )
    for path, n in rows:
        counts[path or scheme.unsorted] = counts.get(path or scheme.unsorted, 0) + int(n)
    for (placement,) in (
        session.query(LibraryItem.placement)
        .filter(LibraryItem.site == site, LibraryItem.deleted_at.is_(None))
        .all()
    ):
        p = placement or {}
        if p.get("needs_look"):
            looks[p.get("path") or scheme.unsorted] = looks.get(p.get("path") or scheme.unsorted, 0) + 1

    out: dict[str, dict[str, Any]] = {}

    def add(path: str, label: str, kind: str, description: str = "") -> None:
        if path in out:
            return
        o = folders.get(path)
        parent = path.rpartition("/")[0]
        if o is not None and o.kind == "override":
            label = o.label or label
            parent = o.shown_under if o.shown_under != "" else parent
        out[path] = {
            "path": path,
            "label": label,
            "kind": kind,
            "parent": parent,
            "description": description,
            "count": counts.get(path, 0),
            "needs_look": looks.get(path, 0),
            "id": o.id if o is not None else None,
        }

    for c in scheme.categories:
        add(c.path, c.label, "scheme", c.description)
    for f in folders.values():
        if f.kind == "user":
            parent = f.shown_under or f.path.rpartition("/")[0]
            if parent == USER_ROOT:
                add(USER_ROOT, "My folders", "user-root")
            out[f.path] = {
                "path": f.path, "label": f.label, "kind": "user", "parent": parent, "description": "",
                "count": counts.get(f.path, 0), "needs_look": looks.get(f.path, 0), "id": f.id,
            }
    # Folders that exist because items filed there (templated paths: by code, by instrument).
    for path in list(counts):
        if path == scheme.unsorted or path in out:
            continue
        parts = path.split("/")
        for i in range(1, len(parts) + 1):
            p = "/".join(parts[:i])
            add(p, scheme.label_for(p), "dynamic")
    # A parent's count includes what is filed below it.
    totals = {p: f["count"] for p, f in out.items()}
    for p in sorted(out, key=lambda x: -x.count("/")):
        parent = out[p]["parent"]
        if parent in totals and parent != p:
            totals[parent] += totals[p]
    for p, f in out.items():
        f["total"] = totals[p]
    return {
        "scheme": {"name": scheme.name, "display_name": scheme.display_name, "version": scheme.version, "source": scheme.source},
        "folders": sorted(out.values(), key=lambda f: (f["path"].count("/"), _order(scheme, f["path"]), f["label"])),
        "unsorted": {
            "path": scheme.unsorted,
            "label": UNSORTED_LABEL,
            "count": counts.get(scheme.unsorted, 0),
            "needs_look": looks.get(scheme.unsorted, 0),
        },
    }


def _order(scheme: BookScheme, path: str) -> int:
    paths = [c.path for c in scheme.categories]
    return paths.index(path) if path in paths else len(paths)


def create_folder(session: Any, site: str, principal: str, label: str, parent: str = "") -> dict[str, Any]:
    from axiom.extensions.builtins.library.db_models import LibraryFolder

    label = " ".join((label or "").split())[:128]
    if not label:
        raise ValueError("a folder needs a name")
    parent = (parent or "").strip("/") or USER_ROOT
    base = f"{parent}/{slug(label)}"
    path, n = base, 2
    existing = _overrides(session, site)
    scheme_paths = {c.path for c in active_scheme().categories}
    while path in existing or path in scheme_paths:
        path, n = f"{base}-{n}", n + 1
    f = LibraryFolder(
        id=str(uuid.uuid4()), site=site, path=path, label=label, kind="user", shown_under="",
        created_by=principal, created_at=_now(), updated_at=_now(),
    )
    session.add(f)
    return {"id": f.id, "path": path, "label": label, "kind": "user", "parent": parent}


def change_folder(session: Any, site: str, principal: str, path: str, *, label: str | None, parent: str | None) -> dict[str, Any]:
    from axiom.extensions.builtins.library.db_models import LibraryFolder

    existing = _overrides(session, site)
    scheme = active_scheme()
    f = existing.get(path)
    if f is None:
        if path not in scheme.by_path:
            raise LookupError(f"no folder {path!r} in this Book")
        f = LibraryFolder(
            id=str(uuid.uuid4()), site=site, path=path, label=scheme.label_for(path), kind="override",
            shown_under="", created_by=principal, created_at=_now(), updated_at=_now(),
        )
        session.add(f)
    if label is not None:
        clean = " ".join(label.split())[:128]
        if not clean:
            raise ValueError("a folder needs a name")
        f.label = clean
    if parent is not None:
        target = parent.strip("/")
        if target == path or target.startswith(path + "/"):
            raise ValueError("a folder cannot be moved inside itself")
        f.shown_under = target
    f.updated_at = _now()
    return {"id": f.id, "path": f.path, "label": f.label, "kind": "user" if f.kind == "user" else "scheme", "parent": f.shown_under or path.rpartition("/")[0]}


def delete_folder(session: Any, site: str, path: str) -> None:
    from axiom.extensions.builtins.library.db_models import LibraryFolder, LibraryItem

    f = (
        session.query(LibraryFolder)
        .filter(LibraryFolder.site == site, LibraryFolder.path == path, LibraryFolder.kind == "user", LibraryFolder.deleted_at.is_(None))
        .one_or_none()
    )
    if f is None:
        raise LookupError(f"no folder of yours at {path!r} (scheme folders cannot be deleted, only renamed or moved)")
    filed = session.query(LibraryItem).filter(LibraryItem.site == site, LibraryItem.folder == path, LibraryItem.deleted_at.is_(None)).count()
    if filed:
        raise ValueError(f"{f.label!r} still holds {filed} item(s); move them first")
    f.deleted_at = _now()


def folder_exists(session: Any, site: str, path: str) -> bool:
    scheme = active_scheme()
    if path in scheme.by_path or path == scheme.unsorted:
        return True
    from axiom.extensions.builtins.library.db_models import LibraryItem

    if path in _overrides(session, site):
        return True
    return session.query(LibraryItem).filter(LibraryItem.site == site, LibraryItem.folder == path).count() > 0


def record_move(session: Any, item: Any, principal: str, to_path: str) -> dict[str, Any]:
    """Move one item: file it there, pin it (a re-extraction will not re-file it), and learn from it."""
    from axiom.extensions.builtins.library.db_models import LibraryCorrection

    scheme = active_scheme()
    before = item.folder or scheme.unsorted
    placement = dict(item.placement or {})
    sig = placement.get("signature") or []
    session.add(
        LibraryCorrection(site=item.site, item_id=item.id, principal=principal, from_path=before, to_path=to_path, signature=sig, at=_now())
    )
    labels = _user_labels(session, item.site, to_path, scheme)
    placement.update(
        {
            "path": to_path,
            "labels": labels,
            "confidence": 1.0,
            "source": "manual",
            "needs_look": False,
            "because": f"{principal} moved it here",
            "explanation": f"Filed under {' / '.join(labels)} because {principal} moved it here.",
            "moved_from": before,
        }
    )
    item.folder = to_path
    item.placement = placement
    return placement


__all__ = [
    "BOOK_ENV",
    "USER_ROOT",
    "active_scheme",
    "change_folder",
    "corrections",
    "create_folder",
    "delete_folder",
    "file_item",
    "folder_exists",
    "placement_json",
    "record_move",
    "schemes",
    "tree",
    "user_folders",
]
