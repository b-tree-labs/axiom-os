# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The browser's common views: Starred, Recent, Shared with me, As uploaded.

The Library browser opens on a column of views every person has, above the
product's own Book: All files, Recent, Starred, Shared with me, Deleted files
and As uploaded. All files and Deleted files are :func:`.service.list_items`.
The other four are here.

**Per person.** A star and an open belong to the principal who made them, at
the site they made them in (:class:`.db_models.LibraryMark`). Nobody else's
Starred or Recent shows them, and a site never sees another site's marks.
Every listing re-checks the item through :func:`.access.readable_ids`, so a star on a
document that has since gone private to someone else, or been deleted, is
kept but not served.

**Shared with me.** Person-to-person grants (ADR-208/209) are not built yet.
Until they are, "shared with me" is what a colleague filed for the site: an
item at the caller's site, visible to the site, that someone else owns. The
answer says so (``basis: "site"``) so the page can tell the person where the
list comes from rather than imply a grant that does not exist.

**As uploaded.** The provenance listing: every file exactly as it arrived, by
the name it was given, with the hash bronze holds it under, who sent it and
through which surface. Read-only: the only action on a row is downloading the
original bytes, which come from bronze and are checksum-verified on the way
out (:func:`.service.read_content`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any



from axiom.extensions.builtins.library.db_models import LibraryEvent, LibraryItem, LibraryMark
from axiom.extensions.builtins.library import access
from axiom.extensions.builtins.library.scope import Caller
from axiom.extensions.builtins.library.service import MAX_LIST, NotFound, _visible, to_dict
from axiom.extensions.builtins.library.store import session_scope

#: How many Recent entries a person gets; Home shows the first few.
RECENT_MAX = 50
#: Page size ceiling for As uploaded.
ORIGINALS_PAGE_MAX = 100

#: Where the HTTP face serves the library, for download links.
DEFAULT_BASE = "/api/v1/library"

KIND_ITEM = "item"
KIND_FOLDER = "folder"

SHARED_BASIS_SITE = "site"
#: Shared with the person, or a group of theirs, by name (ADR-263).
SHARED_BASIS_DIRECT = "shared"
SHARED_NOTE = (
    "Shared with you by name first, newest share first; then what colleagues filed for everyone at your site. "
    "The site rows leave this view when the web's sharing lands (prd-sharing-and-access SHR-R9.2)."
)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _when(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _mark(session: Any, caller: Caller, kind: str, target: str) -> LibraryMark:
    found = (
        session.query(LibraryMark)
        .filter(
            LibraryMark.site == caller.site,
            LibraryMark.principal == caller.principal,
            LibraryMark.kind == kind,
            LibraryMark.target == target,
        )
        .one_or_none()
    )
    if found is None:
        found = LibraryMark(
            site=caller.site, principal=caller.principal, kind=kind, target=target, open_count=0
        )
        session.add(found)
    return found


def _folder_known(session: Any, caller: Caller, path: str) -> str:
    from axiom.extensions.builtins.library.book import service as book

    clean = path.strip("/")
    if not clean or not book.folder_exists(session, caller.site, clean):
        raise NotFound(f"no Data Book folder {path!r}")
    return clean


# ------------------------------------------------------------------------ stars


def set_item_star(caller: Caller, item_id: str, starred: bool) -> dict[str, Any]:
    """Star or unstar a document for this person (idempotent). 404 when they cannot read it."""
    with session_scope() as session:
        item = _visible(session, caller, item_id)
        mark = _mark(session, caller, KIND_ITEM, item.id)
        mark.starred_at = (mark.starred_at or _now()) if starred else None
        session.commit()
        return {"id": item.id, "starred": starred}


def set_folder_star(caller: Caller, path: str, starred: bool) -> dict[str, Any]:
    """Star or unstar a Data Book folder for this person (idempotent)."""
    with session_scope() as session:
        clean = _folder_known(session, caller, path)
        mark = _mark(session, caller, KIND_FOLDER, clean)
        mark.starred_at = (mark.starred_at or _now()) if starred else None
        session.commit()
        return {"path": clean, "starred": starred}


def starred(caller: Caller) -> dict[str, Any]:
    """This person's starred documents (newest star first) and folders.

    A deleted document drops out until it is restored; one the person can no
    longer read never appears.
    """
    with session_scope() as session:
        marks = (
            session.query(LibraryMark)
            .filter(
                LibraryMark.site == caller.site,
                LibraryMark.principal == caller.principal,
                LibraryMark.starred_at.isnot(None),
            )
            .all()
        )
        marks.sort(key=lambda m: _when(m.starred_at), reverse=True)
        item_ids = [m.target for m in marks if m.kind == KIND_ITEM]
        rows = (
            {r.id: r for r in session.query(LibraryItem).filter(LibraryItem.id.in_(item_ids)).all()}
            if item_ids
            else {}
        )
        items: list[dict[str, Any]] = []
        seen = access.readable_ids(session, caller, rows.values())
        for m in marks:
            if m.kind != KIND_ITEM:
                continue
            row = rows.get(m.target)
            if row is None or row.deleted_at is not None or row.id not in seen:
                continue
            items.append({**to_dict(row), "starred_at": _iso(m.starred_at)})
        folders = [
            {"path": m.target, "starred_at": _iso(m.starred_at)}
            for m in marks
            if m.kind == KIND_FOLDER
        ]
    return {"items": items[:MAX_LIST], "folders": folders}


# ------------------------------------------------------------------------ recent


def record_open(caller: Caller, item_id: str) -> dict[str, Any]:
    """Note that this person opened a document, for their Recent."""
    with session_scope() as session:
        item = _visible(session, caller, item_id)
        mark = _mark(session, caller, KIND_ITEM, item.id)
        mark.opened_at = _now()
        mark.open_count = int(mark.open_count or 0) + 1
        session.commit()
        return {"id": item.id, "opened_at": _iso(mark.opened_at)}


def recent(caller: Caller, *, limit: int = RECENT_MAX) -> list[dict[str, Any]]:
    """What this person opened or uploaded, most recent first.

    Each entry carries ``recent_at`` and ``recent_reason`` (``opened`` or
    ``uploaded``), whichever is later. A colleague's opens never count.
    """
    cap = max(1, min(int(limit), RECENT_MAX))
    with session_scope() as session:
        opened = (
            session.query(LibraryMark)
            .filter(
                LibraryMark.site == caller.site,
                LibraryMark.principal == caller.principal,
                LibraryMark.kind == KIND_ITEM,
                LibraryMark.opened_at.isnot(None),
            )
            .order_by(LibraryMark.opened_at.desc())
            .limit(cap * 2)
            .all()
        )
        mine = (
            session.query(LibraryItem)
            .filter(
                LibraryItem.site == caller.site,
                LibraryItem.owner == caller.principal,
                LibraryItem.deleted_at.is_(None),
            )
            .order_by(LibraryItem.created_at.desc())
            .limit(cap * 2)
            .all()
        )
        when: dict[str, tuple[datetime, str]] = {}
        for item in mine:
            when[item.id] = (_when(item.created_at), "uploaded")
        for m in opened:
            at = _when(m.opened_at)
            if m.target not in when or at > when[m.target][0]:
                when[m.target] = (at, "opened")
        ids = list(when)
        rows = (
            {r.id: r for r in session.query(LibraryItem).filter(LibraryItem.id.in_(ids)).all()}
            if ids
            else {}
        )
        out: list[dict[str, Any]] = []
        seen = access.readable_ids(session, caller, rows.values())
        for item_id, (at, reason) in sorted(when.items(), key=lambda kv: kv[1][0], reverse=True):
            row = rows.get(item_id)
            if row is None or row.deleted_at is not None or row.id not in seen:
                continue
            out.append({**to_dict(row), "recent_at": _iso(at), "recent_reason": reason})
            if len(out) >= cap:
                break
    return out


# ------------------------------------------------------------------------ shared with me


def shared_with_me(caller: Caller, *, limit: int = 200) -> dict[str, Any]:
    """What was shared with this person or their groups, newest share first, then their site's files."""
    from axiom.governance.access import store as access_store

    cap = max(1, min(int(limit), MAX_LIST))
    person = access.who_of(caller)
    with access_store.session_scope() as s:
        grants = [g for g in access_store.objects_shared_with(s, caller.site, person.subjects, kind="document")]
    with session_scope() as session:
        direct_rows = (
            {r.id: r for r in session.query(LibraryItem).filter(
                LibraryItem.id.in_([g.object_id for g in grants]), LibraryItem.deleted_at.is_(None)).all()}
            if grants else {}
        )
        direct_seen = access.readable_ids(session, caller, direct_rows.values())
        direct: list[dict[str, Any]] = []
        listed: set[str] = set()
        for g in grants:
            r = direct_rows.get(g.object_id)
            if r is None or r.id in listed or r.id not in direct_seen or r.owner == caller.principal:
                continue
            listed.add(r.id)
            direct.append({**to_dict(r), "shared_by": g.granted_by, "shared_at": _iso(g.granted_at),
                           "shared_via": SHARED_BASIS_DIRECT, "role": g.role.value,
                           "through": g.subject if g.subject != caller.principal else None})
        rows = (
            session.query(LibraryItem)
            .filter(
                LibraryItem.site == caller.site,
                LibraryItem.visibility == "site",
                LibraryItem.owner != caller.principal,
                LibraryItem.deleted_at.is_(None),
            )
            .order_by(LibraryItem.created_at.desc())
            .limit(cap)
            .all()
        )
        site_seen = access.readable_ids(session, caller, rows)
        items = direct + [
            {
                **to_dict(r),
                "shared_by": r.owner,
                "shared_at": _iso(r.created_at),
                "shared_via": SHARED_BASIS_SITE,
            }
            for r in rows
            if r.id in site_seen and r.id not in listed
        ]
    return {"items": items[:cap], "basis": SHARED_BASIS_SITE, "grants": True, "note": SHARED_NOTE}


# ------------------------------------------------------------------------ as uploaded


def originals(
    caller: Caller,
    *,
    page: int = 1,
    page_size: int = 50,
    job: str = "",
    base: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Every file exactly as it came in, newest first: the provenance listing.

    ``job`` narrows to one upload (the upload tray's "View original"). A row
    names the file as it was given, never a generated title.
    """
    size = max(1, min(int(page_size), ORIGINALS_PAGE_MAX))
    number = max(1, int(page))
    with session_scope() as session:
        # The access pre-filter, in SQL so the page and the total are counted the same way.
        query = session.query(LibraryItem).filter(
            LibraryItem.site == caller.site,
            LibraryItem.deleted_at.is_(None),
            access.sql_scope(session, caller),
        )
        if job:
            query = query.filter(LibraryItem.job_id == job)
        total = query.count()
        page_rows = query.order_by(LibraryItem.created_at.desc()).offset((number - 1) * size).limit(size).all()
        page_seen = access.readable_ids(session, caller, page_rows)
        window = [r for r in page_rows if r.id in page_seen]
        ids = [r.id for r in window]
        uploads: dict[str, LibraryEvent] = {}
        if ids:
            for ev in (
                session.query(LibraryEvent)
                .filter(LibraryEvent.item_id.in_(ids), LibraryEvent.action == "upload")
                .order_by(LibraryEvent.at.asc())
                .all()
            ):
                uploads.setdefault(ev.item_id, ev)
        items = []
        for r in window:
            ev = uploads.get(r.id)
            detail = dict(ev.detail or {}) if ev is not None else {}
            items.append(
                {
                    "kind": "document",
                    "item_id": r.id,
                    "file_name": detail.get("file_name") or r.file_name,
                    "size": r.size,
                    "mime_type": r.mime_type,
                    "sha256": r.sha256,
                    "uploaded_at": _iso(r.created_at),
                    "uploaded_by": r.owner,
                    "source": (ev.surface if ev is not None and ev.surface else "upload"),
                    "job_id": r.job_id or None,
                    "visibility": r.visibility,
                    # Its labels travel with it, as on every other row (MVP-04-A1):
                    # the classification label and the export-control verdict, so a
                    # held file reads as held here too.
                    "label": r.label,
                    "ec": dict(r.ec or {}),
                    "download_href": f"{base}/items/{r.id}/content?download=true",
                }
            )
    return {"items": items, "total": total, "page": number, "page_size": size}


__all__ = [
    "DEFAULT_BASE",
    "RECENT_MAX",
    "SHARED_NOTE",
    "originals",
    "recent",
    "record_open",
    "set_folder_star",
    "set_item_star",
    "shared_with_me",
    "starred",
]
