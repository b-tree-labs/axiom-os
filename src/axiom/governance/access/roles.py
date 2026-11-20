# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The share roles, as a ladder (ADR-263 D1, prd-sharing-and-access SHR-R1.3).

Google Drive's words, on one object at a time: Owner > Editor > Commenter >
Viewer. An agent adds Invoker between Commenter and Editor (SHR-R11.1); it is in
the ladder so the storage carries it, and refused on a document or a folder.

These are never the site roles in :mod:`axiom.governance.roles` (``reader``,
``site-admin``): a share role is on one object, a site role is at a site. The
code names them ``viewer``, ``commenter``, ``invoker``, ``editor`` and
``owner`` and never puts them on the FGA ``site`` type (PRD §4, term collisions).
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    VIEWER = "viewer"
    COMMENTER = "commenter"
    INVOKER = "invoker"
    EDITOR = "editor"
    OWNER = "owner"

    @property
    def rank(self) -> int:
        return _RANK[self]

    @property
    def label(self) -> str:
        return self.value.capitalize()

    def at_least(self, other: Role) -> bool:
        return self.rank >= other.rank

    @classmethod
    def parse(cls, raw: object) -> Role:
        value = str(getattr(raw, "value", raw) or "").strip().lower()
        try:
            return cls(value)
        except ValueError:
            names = ", ".join(r.value for r in cls if r is not cls.OWNER)
            raise ValueError(f"{raw!r} is not a role; use one of {names}") from None


_RANK = {Role.VIEWER: 1, Role.COMMENTER: 2, Role.INVOKER: 3, Role.EDITOR: 4, Role.OWNER: 5}

#: Object kinds this stage knows. ``agent`` arrives with the agent stage.
DOCUMENT = "document"
FOLDER = "folder"
KINDS = (DOCUMENT, FOLDER)

#: What a person can be given on a document or a folder: never Owner (that is a
#: transfer), never Invoker (an agent's role).
GRANTABLE = {
    DOCUMENT: (Role.VIEWER, Role.COMMENTER, Role.EDITOR),
    FOLDER: (Role.VIEWER, Role.COMMENTER, Role.EDITOR),
}

#: General access (SHR-R2.1): Restricted, or Everyone at the site with a role.
RESTRICTED = "restricted"
SITE = "site"
GENERAL = (RESTRICTED, SITE)

#: The site role that may rename, move or hide a Data Book scheme category for
#: the site (SHR-R4.4). It gives no access to any file. ``site-admin`` holds it.
BOOK_MANAGER = "book-manager"

#: A group is named on an access list as ``group:<directory group id>`` (ADR-103).
GROUP_PREFIX = "group:"


def best(roles: object) -> Role | None:
    """The highest of ``roles`` (an iterable of :class:`Role`), or ``None``."""
    found = [r for r in roles if r is not None]  # type: ignore[attr-defined]
    return max(found, key=lambda r: r.rank) if found else None


def capped(role: Role | None, cap: Role) -> Role | None:
    """``role``, but never above ``cap``: ownership of a folder does not own what it holds."""
    if role is None:
        return None
    return cap if role.rank > cap.rank else role


__all__ = [
    "BOOK_MANAGER",
    "DOCUMENT",
    "FOLDER",
    "GENERAL",
    "GRANTABLE",
    "GROUP_PREFIX",
    "KINDS",
    "RESTRICTED",
    "Role",
    "SITE",
    "best",
    "capped",
]
