# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``sites.list`` / ``sites.show``: the tenants the asker may see, and nothing else.

These are what the scope selector (MVP-15) and "which sites can I see" answer
from. A site the asker may not read is not listed, not counted and not named:
``sites.show`` answers for it exactly as for a site that does not exist
(MVP-10-A1), because a refusal that differs from "not found" says the site is
hosted here.

The candidates are what the node serves (the tenant registry and
``AXIOM_SERVED_SITES``, :mod:`axiom.infra.tenants`) plus its own home site, and
each is kept only when the one decision function lets this caller read it
(:func:`axiom.infra.site_scope.resolve`).
"""

from __future__ import annotations

import os
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def visible_sites() -> list[str]:
    """The sites the current caller may read, sorted; the selector's choices."""
    from axiom.infra import site_scope, tenants
    from axiom.infra.site_authority import is_home_site

    scope = site_scope.resolve(selection=False)
    if not scope.unbounded:
        return sorted(scope.sites)
    candidates = set(tenants.ids())
    for name in [os.environ.get("AXIOM_SITE", "")] + os.environ.get("AXIOM_HOME_SITES", "").split(","):
        if name.strip() and is_home_site(name.strip()):
            candidates.add(name.strip())
    return sorted(s for s in candidates if scope.permits(s))


def _row(site: str, chosen: frozenset[str] | None) -> dict:
    from axiom.infra import tenants

    return {"id": site, "name": tenants.label(site),
            "selected": chosen is None or site in chosen}


def list_sites(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    del params, ctx
    from axiom.infra.site_authority import selected_sites

    sites = visible_sites()
    chosen = selected_sites()
    kept = None if chosen is None else frozenset(s for s in chosen if s in sites)
    return SkillResult(
        ok=True,
        value={
            "sites": [_row(s, kept) for s in sites],
            "selected": sorted(kept) if kept else [],
            # One site, or none: nothing to choose, so no selector (MVP-15-A2).
            "show_selector": len(sites) > 1,
        },
        actions_taken=[f"{len(sites)} site(s) visible to you"],
    )


def show_site(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    del ctx
    from axiom.infra import tenants
    from axiom.infra.site_identity import resolve

    asked = resolve(str(params.get("site") or "").strip())
    if not asked:
        return SkillResult(ok=False, errors=["name the site: site is required"])
    if asked not in visible_sites():
        # The same words for "not hosted" and "not yours" (MVP-10-A1).
        return SkillResult(ok=False, errors=[f"no site {asked!r} here"])
    tenant = next((t for t in tenants.load() if t.id == asked), None)
    record = tenant.to_dict() if tenant else {"id": asked, "name": asked, "also_known_as": []}
    return SkillResult(ok=True, value=record)


__all__ = ["list_sites", "show_site", "visible_sites"]
