# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Who sees which fields of an actor (MVP-23-A5, PRD site-view §20.3).

Three viewer classes, decided from the viewer's roles:

| class | who | sees of a person |
|---|---|---|
| ``member`` | anyone signed in at the site | name, handle, kind, roles, site, contact buttons |
| ``steward`` | supervisors and site managers | + status (active, suspended, invited), directory groups |
| ``admin`` | owner, admin, operator | + the address, keys, the pending invitation |

The fields are removed on the server, before anything leaves it: a field a
viewer may not see is absent from the row, not blank. Contact is a list of the
channels a person can be reached on (shown as buttons); the address itself is an
admin field.

Which roles are stewards is site configuration, because the platform has no
vocabulary for a site's supervisors: ``AXIOM_TEAM_STEWARD_ROLES`` (comma list,
default ``supervisor,manager``). ``AXIOM_TEAM_ADMIN_ROLES`` adds to the built-in
administrators.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

from axiom.governance.roles import canonical_roles

MEMBER, STEWARD, ADMIN = "member", "steward", "admin"
CLASSES = (MEMBER, STEWARD, ADMIN)

#: Current ids; a viewer's roles are read through ``canonical_roles``, so ``operator`` is
#: ``site-admin`` (ADR-261).
ADMIN_ROLES = frozenset({"owner", "admin", "site-admin"})
DEFAULT_STEWARD_ROLES = ("supervisor", "manager")

#: Every field a row may carry, and the lowest class that sees it.
FIELDS: dict[str, str] = {
    "kind": MEMBER,
    "kind_word": MEMBER,
    "shape": MEMBER,
    "handle": MEMBER,
    "name": MEMBER,
    "roles": MEMBER,
    "site": MEMBER,
    "contact": MEMBER,
    "about": MEMBER,
    "accountable": MEMBER,
    "finding": MEMBER,
    "serves": MEMBER,
    "status": STEWARD,
    "groups": STEWARD,
    "email": ADMIN,
    "user_id": ADMIN,
    "keys": ADMIN,
    "invitation": ADMIN,
    "source": ADMIN,
}

_RANK = {MEMBER: 0, STEWARD: 1, ADMIN: 2}


def _roles(env_key: str, default: Iterable[str]) -> frozenset[str]:
    raw = os.environ.get(env_key)
    if raw is None:
        return frozenset(canonical_roles(default))
    return frozenset(canonical_roles(r.strip() for r in raw.split(",") if r.strip()))


def viewer_class(roles: Iterable[str]) -> str:
    held = set(canonical_roles(roles or ()))
    if held & (ADMIN_ROLES | _roles("AXIOM_TEAM_ADMIN_ROLES", ())):
        return ADMIN
    if held & _roles("AXIOM_TEAM_STEWARD_ROLES", DEFAULT_STEWARD_ROLES):
        return STEWARD
    return MEMBER


def project(row: dict, viewer: str) -> dict:
    """``row`` with only the fields ``viewer`` may see."""
    rank = _RANK[viewer]
    return {k: v for k, v in row.items() if _RANK[FIELDS.get(k, ADMIN)] <= rank}


def visible_fields(viewer: str) -> list[str]:
    rank = _RANK[viewer]
    return [k for k, c in FIELDS.items() if _RANK[c] <= rank]


__all__ = ["ADMIN", "CLASSES", "FIELDS", "MEMBER", "STEWARD", "project", "viewer_class",
           "visible_fields"]
