# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""When a person leaves a site, their Library files and folders go to a named person (SHR-R5.3).

A site-admin or an access admin runs it, through this step only, with no second
approval (decided by the owner, PRD §9 decision 13). Every file (live or in
Deleted files) and every folder the person owns at the site gets the named
person as its Owner, so nothing is left without one; entries others hold stay,
so nobody loses access they had through them. Each transfer is a ``decide()``
receipt and a line in the item's activity, and the new Owner is told. The
administrator gains no role on any of it (ADR-142 rule 8): the verb opens nothing.
"""

from __future__ import annotations

from typing import Any

from axiom.extensions.builtins.library import access
from axiom.extensions.builtins.library.scope import Caller
from axiom.extensions.builtins.library.store import session_scope


def offboard(caller: Caller, person: str, to: str) -> dict[str, Any]:
    from axiom.extensions.builtins.library import people, service
    from axiom.extensions.builtins.library.db_models import LibraryEvent, LibraryFolder, LibraryItem
    from axiom.extensions.builtins.library.scope import make_caller
    from axiom.extensions.builtins.library.sharing import _tell

    leaving = people.resolve(caller, person)
    heir = people.resolve(caller, to)
    if not leaving or not heir:
        raise ValueError("say who is leaving and who takes their files")
    if leaving == heir:
        raise ValueError("hand the files to someone else than the person who is leaving")
    files = folders_moved = 0
    with session_scope() as session:
        live = access.user_folders(session, caller.site)
        items = session.query(LibraryItem).filter(LibraryItem.site == caller.site, LibraryItem.owner == leaving).all()
        folders = (
            session.query(LibraryFolder)
            .filter(LibraryFolder.site == caller.site, LibraryFolder.kind == "user", LibraryFolder.created_by == leaving)
            .all()
        )
        words = f"{caller.principal} handed {leaving}'s file to {heir} (offboarding)"
        for item in items:
            decision = access.require(session, caller, "library.offboard", access.item_object(item, live))
            item.owner = heir
            if item.visibility != "site" and item.deleted_at is None:
                # A Restricted file's chunks live in its Owner's own corpus: they move with it.
                service._unindex(item)  # noqa: SLF001
                item.corpus = service.corpus_for(make_caller(heir, item.site), "private")
                service._reindex_from_bronze(item)  # noqa: SLF001
            session.add(LibraryEvent(item_id=item.id, site=item.site, principal=caller.principal, action="transfer",
                                     surface=caller.surface, at=service._now(),  # noqa: SLF001
                                     detail={"words": words, "owner": heir, "was": leaving, "offboarding": True,
                                             "receipt_id": decision.receipt_id}))
            files += 1
        for folder in folders:
            decision = access.require(session, caller, "library.offboard", access.folder_object(folder, live))
            folder.created_by = heir
            session.add(LibraryEvent(item_id=folder.id, site=folder.site, principal=caller.principal, action="transfer",
                                     surface=caller.surface, at=service._now(),  # noqa: SLF001
                                     detail={"words": f"{caller.principal} handed {leaving}'s folder to {heir} (offboarding)",
                                             "owner": heir, "was": leaving, "offboarding": True,
                                             "receipt_id": decision.receipt_id}))
            folders_moved += 1
        session.commit()
    if files or folders_moved:
        _tell(heir, f"You now own {files} file{'s' if files != 1 else ''} and {folders_moved} folder"
                    f"{'s' if folders_moved != 1 else ''} that were {leaving}'s",
              f"{caller.principal} handed them to you as {leaving} leaves the site.", f"offboard:{leaving}:{heir}")
    return {"person": leaving, "to": heir, "files": files, "folders": folders_moved}


__all__ = ["offboard"]
