# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The Library's side of the one decision point (ADR-263 D9; prd-sharing-and-access SHR-R1).

The Library knows what only it can know about a document or a folder: who owns
it (``library_items.owner``, ``library_folders.created_by``), which of a
person's folders enclose it, its general access before sharing existed
(``visibility``: ``site`` is Everyone at the site · Viewer, ``private`` is
Restricted), whether it is held for export-control review, and who deleted it.
This module hands those facts to :mod:`axiom.governance.access` and asks it;
every Library verb, on every surface, goes through :func:`require`, and every
listing filters through :func:`readable_ids`.

``visibility`` is kept and written in step as the projection of general access
(PRD §8), because retrieval still picks its corpus from it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from axiom.extensions.builtins.library.scope import Caller
from axiom.governance.access import AccessObject, NoAccess, Refused, Role, Who
from axiom.governance.access import decision as _decision
from axiom.governance.access.roles import DOCUMENT, FOLDER, RESTRICTED, SITE

#: The Library's not-found sentence for an item a person cannot open: the address alone reveals nothing.
NO_ACCESS = "You need access. Ask for it, and the owner is told."


def who_of(caller: Caller) -> Who:
    return _decision.who(caller.principal, caller.site)


def _general(visibility: str | None) -> str:
    return SITE if (visibility or "site") == "site" else RESTRICTED


def visibility_for(general: str) -> str:
    """The ``visibility`` projection of a general access line."""
    return "site" if general == SITE else "private"


def user_folders(session: Any, site: str) -> dict[str, Any]:
    """Every live person's folder at ``site``, by path."""
    from axiom.extensions.builtins.library.db_models import LibraryFolder

    rows = (
        session.query(LibraryFolder)
        .filter(LibraryFolder.site == site, LibraryFolder.kind == "user", LibraryFolder.deleted_at.is_(None))
        .all()
    )
    return {r.path: r for r in rows}


def _enclosing(path: str, folders: dict[str, Any], *, include_self: bool) -> list[str]:
    """The person's-folder paths that hold ``path``, nearest first."""
    parts = (path or "").strip("/").split("/") if path else []
    out = []
    for i in range(len(parts), 0, -1):
        p = "/".join(parts[:i])
        if (include_self or p != path) and p in folders:
            out.append(p)
    return out


def folder_object(row: Any, folders: dict[str, Any]) -> AccessObject:
    parents = tuple(_folder_object_plain(folders[p]) for p in _enclosing(row.path, folders, include_self=False))
    return AccessObject(kind=FOLDER, id=row.id, site=row.site, owner=row.created_by or "",
                        legacy_general=RESTRICTED, parents=parents)


def _folder_object_plain(row: Any) -> AccessObject:
    return AccessObject(kind=FOLDER, id=row.id, site=row.site, owner=row.created_by or "", legacy_general=RESTRICTED)


def item_object(item: Any, folders: dict[str, Any]) -> AccessObject:
    parents = tuple(_folder_object_plain(folders[p]) for p in _enclosing(item.folder or "", folders, include_self=True))
    return AccessObject(
        kind=DOCUMENT, id=item.id, site=item.site, owner=item.owner or "",
        legacy_general=_general(item.visibility), parents=parents,
        held=(getattr(item, "ec", None) or {}).get("verdict") == "held",
        deleted_by=item.deleted_by or "",
        label=_confirmed_label(item),
    )


def _confirmed_label(item: Any) -> str | None:
    from axiom.extensions.builtins.library import labels

    found = labels.confirmed(item)
    return found.classification.value if found is not None else None


def category_object(site: str, path: str) -> AccessObject:
    """A Data Book scheme category: a filing place, owned by nobody, for the Book manager verb."""
    return AccessObject(kind=FOLDER, id=f"scheme:{path}", site=site, owner="", legacy_general=SITE)


def require(session: Any, caller: Caller, verb: str, obj: AccessObject) -> _decision.Decision:
    """The verdict for one Library verb, as the Library's own errors (a receipt either way)."""
    from axiom.extensions.builtins.library.service import Forbidden, NotFound

    try:
        return _decision.require(who_of(caller), verb, obj, surface=caller.surface)
    except NoAccess as exc:
        raise NotFound(NO_ACCESS) from exc
    except Refused as exc:
        raise Forbidden(str(exc)) from exc


def require_item(session: Any, caller: Caller, verb: str, item: Any) -> _decision.Decision:
    return require(session, caller, verb, item_object(item, user_folders(session, item.site)))


def role_of_item(session: Any, caller: Caller, item: Any) -> Role | None:
    return _decision.role_of(who_of(caller), item_object(item, user_folders(session, item.site)))


def may_item(session: Any, caller: Caller, verb: str, item: Any) -> bool:
    return _decision.may(who_of(caller), verb, item_object(item, user_folders(session, item.site)))


def readable_ids(session: Any, caller: Caller, items: Iterable[Any]) -> set[str]:
    """Which of ``items`` the caller may see, in one read of the record."""
    rows = [r for r in items if r is not None and r.site == caller.site]
    if not rows:
        return set()
    folders = user_folders(session, caller.site)
    roles = _decision.roles_of(who_of(caller), [item_object(r, folders) for r in rows])
    return {key[1] for key, role in roles.items() if role is not None}


def readable_one(caller: Caller, item: Any) -> bool:
    from axiom.extensions.builtins.library.store import session_scope

    with session_scope() as session:
        return item.id in readable_ids(session, caller, [item])


def may_one(caller: Caller, verb: str, item: Any) -> bool:
    from axiom.extensions.builtins.library.store import session_scope

    with session_scope() as session:
        return may_item(session, caller, verb, item)


def shared_scope(session: Any, caller: Caller) -> tuple[set[str], set[str]]:
    """What is shared with the caller beyond general access: item ids, and folder paths (whose contents it reaches).

    The SQL pre-filter for listings, so a page counts the rows a person may see;
    the exact answer is still :func:`readable_ids`.
    """
    from axiom.governance.access import store

    person = who_of(caller)
    with store.session_scope() as s:
        grants = store.objects_shared_with(s, caller.site, person.subjects)
    item_ids = {g.object_id for g in grants if g.kind == DOCUMENT}
    folder_ids = {g.object_id for g in grants if g.kind == FOLDER}
    folders = user_folders(session, caller.site)
    paths = {p for p, row in folders.items() if row.id in folder_ids or (row.created_by == caller.principal)}
    return item_ids, paths


def sql_scope(session: Any, caller: Caller) -> Any:
    """A SQL clause no narrower than what the caller may see: general access, their own, and their shares.

    It only pre-filters, so a page and its cursor count about the right rows; the
    exact answer for each row is :func:`readable_ids`.
    """
    from sqlalchemy import or_

    from axiom.extensions.builtins.library.db_models import LibraryItem

    item_ids, paths = shared_scope(session, caller)
    terms = [LibraryItem.visibility == "site", LibraryItem.owner == caller.principal]
    if item_ids:
        terms.append(LibraryItem.id.in_(sorted(item_ids)))
    for p in sorted(paths):
        terms.append(LibraryItem.folder == p)
        terms.append(LibraryItem.folder.like(p + "/%"))
    return or_(*terms)


def shared_corpora(session: Any, caller: Caller) -> set[str]:
    """The corpora holding Restricted files shared with the caller by name: their Owners' own corpora."""
    from sqlalchemy import or_

    from axiom.extensions.builtins.library.db_models import LibraryItem

    item_ids, paths = shared_scope(session, caller)
    terms = []
    if item_ids:
        terms.append(LibraryItem.id.in_(sorted(item_ids)))
    for p in sorted(paths):
        terms += [LibraryItem.folder == p, LibraryItem.folder.like(p + "/%")]
    if not terms:
        return set()
    rows = (
        session.query(LibraryItem.corpus)
        .filter(LibraryItem.site == caller.site, LibraryItem.deleted_at.is_(None), LibraryItem.visibility != "site",
                LibraryItem.owner != caller.principal, or_(*terms))
        .distinct()
        .all()
    )
    return {c for (c,) in rows if c}


def visible_folders(session: Any, caller: Caller) -> set[str]:
    """The person's-folder paths the caller may see in the tree (their own, and those shared with them)."""
    folders = user_folders(session, caller.site)
    if not folders:
        return set()
    objs = {p: folder_object(r, folders) for p, r in folders.items()}
    roles = _decision.roles_of(who_of(caller), list(objs.values()))
    return {p for p, o in objs.items() if roles.get(o.key) is not None}


__all__ = [
    "NO_ACCESS",
    "category_object",
    "folder_object",
    "item_object",
    "may_item",
    "may_one",
    "readable_ids",
    "readable_one",
    "require",
    "require_item",
    "role_of_item",
    "shared_corpora",
    "shared_scope",
    "user_folders",
    "visibility_for",
    "visible_folders",
    "who_of",
]
