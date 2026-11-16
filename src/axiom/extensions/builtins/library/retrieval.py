# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The Library's source guard: retrieval returns exactly what the decision point says (SHR-R8.3).

A Library chunk is filed under ``library/<site>/<item id>/...``. General access
reaches people through the corpus, as before (a site file's chunks are in the
site corpus); a file shared by name is in its Owner's own corpus, which nobody
else's retrieval names. This guard closes both ends:

- :meth:`LibraryGuard.extra_corpora` names the corpora that hold what was shared
  with a person (the Owners' own corpora), so their search and chat can find it;
- :meth:`LibraryGuard.permitted` keeps a chunk only when its item is live and the
  person may read it, so a file shared with three people is never retrieved
  for a fourth, whatever corpus it sits in. With no principal, only what
  Everyone at the site may read passes.

It reads the Library's catalog through the Library's own session and the record
through the decision point; it never reads another extension's tables.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

PREFIX = "library/"


def _item_id(path: str) -> tuple[str, str] | None:
    parts = path[len(PREFIX):].split("/", 2)
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def _caller(principal: str):
    from axiom.extensions.builtins.library.scope import _handle_site, make_caller

    return make_caller(principal, _handle_site(principal), surface="retrieval")


class LibraryGuard:
    prefix = PREFIX

    def permitted(self, principal: str | None, paths: Iterable[str]) -> set[str]:
        from axiom.extensions.builtins.library import access
        from axiom.extensions.builtins.library.db_models import LibraryItem
        from axiom.extensions.builtins.library.store import session_scope

        wanted: dict[str, list[str]] = {}
        for p in paths:
            ref = _item_id(p)
            if ref is not None:
                wanted.setdefault(ref[1], []).append(p)
        if not wanted:
            return set()
        with session_scope() as session:
            rows = [r for r in session.query(LibraryItem).filter(LibraryItem.id.in_(list(wanted))).all()
                    if r.deleted_at is None]
            if principal:
                caller = _caller(principal)
                ok = access.readable_ids(session, caller, [r for r in rows if r.site == caller.site])
            else:
                # Nobody in particular: what Everyone at the site may read, and nothing held.
                ok = {r.id for r in rows if r.visibility == "site" and (r.ec or {}).get("verdict") != "held"}
        return {p for item_id in ok for p in wanted.get(item_id, [])}

    def extra_corpora(self, principal: str | None) -> set[str]:
        if not principal:
            return set()
        from axiom.extensions.builtins.library import access
        from axiom.extensions.builtins.library.store import session_scope

        with session_scope() as session:
            return access.shared_corpora(session, _caller(principal))


guard: Any = LibraryGuard()

__all__ = ["LibraryGuard", "PREFIX", "guard"]
