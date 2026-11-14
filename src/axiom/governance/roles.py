# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Platform role names: the id a grant stores, the name a person reads, and retired ids (ADR-261).

The role that reads, invokes and governs at a site is ``site-admin``. It was called
``operator`` (ADR-151, ADR-222); the word names a job in consumer domains, so a person
reading it on a platform screen could take it for that job. Its rights did not change.

A grant stored under a retired id keeps working: every place that reads a role reads it
through :func:`canonical_role`, so ``operator`` in an accounts file, a group-to-role map,
an invitation or a roles file means ``site-admin``. Nothing rewrites a stored grant; new
grants are written with the current id, and naming a retired id says so in one line
(:func:`legacy_note`).

The narrow ``admin`` role keeps its id and is shown as "Access admin": it governs access
and reads nothing (ADR-151).
"""

from __future__ import annotations

from collections.abc import Iterable

#: The role that reads, invokes and governs at a site (ADR-151, ADR-153; renamed by ADR-261).
SITE_ADMIN = "site-admin"
#: The role that governs access and reads nothing.
ACCESS_ADMIN = "admin"

#: Retired role id -> the role it now names. Read everywhere a role is read; never written.
LEGACY_ROLE_ALIASES: dict[str, str] = {"operator": SITE_ADMIN}

#: What a person reads for each platform role. A role not listed shows its id.
DISPLAY_NAMES: dict[str, str] = {
    "owner": "Owner",
    ACCESS_ADMIN: "Access admin",
    SITE_ADMIN: "Site admin",
    "creator": "Creator",
    "viewer": "Viewer",
    "guest": "Guest",
    "reader": "Reader",
    "contributor": "Contributor",
}


def canonical_role(role: object) -> str:
    """The current id of ``role``: a retired id maps to its successor, anything else is itself."""
    name = str(role or "").strip()
    return LEGACY_ROLE_ALIASES.get(name, name)


def canonical_roles(roles: Iterable[object] | str | None) -> tuple[str, ...]:
    """``roles`` with retired ids mapped and repeats dropped, in first-seen order."""
    if roles is None:
        return ()
    if isinstance(roles, str):
        roles = [roles]
    out: dict[str, None] = {}
    for r in roles:
        name = canonical_role(r)
        if name:
            out[name] = None
    return tuple(out)


def same_role(a: object, b: object) -> bool:
    """Whether two role ids name one role."""
    return canonical_role(a) == canonical_role(b)


def with_aliases(roles: Iterable[object] | str | None) -> tuple[str, ...]:
    """``roles`` in current ids, plus each retired id that names one of them.

    For a matcher that compares against values somebody stored (a cohort rule that says
    ``role in [operator]``): the holder of ``site-admin`` matches either spelling.
    """
    held = canonical_roles(roles)
    return held + tuple(old for old, new in LEGACY_ROLE_ALIASES.items() if new in held)


def display_name(role: object) -> str:
    """What a person reads for ``role``: "Site admin", "Access admin", or the id itself."""
    name = canonical_role(role)
    return DISPLAY_NAMES.get(name, name)


def role_label(role: object) -> str:
    """The display name with the id to type, e.g. "Site admin (site-admin)"."""
    name = canonical_role(role)
    shown = DISPLAY_NAMES.get(name)
    return f"{shown} ({name})" if shown else name


def legacy_note(role: object) -> str | None:
    """One line when ``role`` is a retired id, naming the role it now is; else ``None``."""
    name = str(role or "").strip()
    new = LEGACY_ROLE_ALIASES.get(name)
    if new is None:
        return None
    return f"the role {name!r} is now called {new!r} ({display_name(new)}); granted as {new}"


__all__ = [
    "ACCESS_ADMIN",
    "DISPLAY_NAMES",
    "LEGACY_ROLE_ALIASES",
    "SITE_ADMIN",
    "canonical_role",
    "canonical_roles",
    "display_name",
    "legacy_note",
    "role_label",
    "same_role",
    "with_aliases",
]
