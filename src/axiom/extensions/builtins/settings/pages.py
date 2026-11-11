# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Account and Settings: two pages, split by whose they are (MVP-08, PRD §20.4).

- **Account** is the signed-in person's own page, from the avatar menu. Everyone
  has it, and nothing on it changes the site.
- **Settings** is the site's configuration, for administrators. Nothing on it
  is personal. Work on people (invitations, roles, the roster) is in Team.

This table is the one place the sections are declared. The web app renders
what :func:`sections_for` returns for the caller's roles, so a section is
present for exactly the roles in the table and absent for every other
(MVP-08-A1), and each section names what it writes and whose that is, so a
test can hold the split (MVP-08-A4).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

#: The roles that administer the site. The same set may change node settings
#: over the web (``settings.api.EDIT_ROLES``).
ADMIN_ROLES: frozenset[str] = frozenset({"owner", "operator", "admin"})

PERSONAL = "personal"
SITE = "site"


@dataclass(frozen=True)
class Section:
    """One section of Account or Settings."""

    id: str
    label: str
    page: str  # "account" or "settings"
    contents: str
    #: ``None`` is everyone signed in; otherwise any one of these roles.
    roles: frozenset[str] | None = None
    #: What the section changes, as (capability or route, whose): every write it offers.
    writes: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    #: Read only in this phase (P1): shown, but nothing changes from it yet.
    read_only: bool = False

    def visible_to(self, roles: Iterable[str]) -> bool:
        return self.roles is None or bool(self.roles & set(roles))

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "page": self.page,
            "contents": self.contents,
            "read_only": self.read_only,
            "writes": [{"target": t, "whose": w} for t, w in self.writes],
        }


ACCOUNT: tuple[Section, ...] = (
    Section("overview", "Overview", "account",
            "What expires soon (keys, qualifications, grants) first"),
    Section("profile", "Profile", "account",
            "Name, photo, contact (verified channels only), time zone, public-site listing",
            writes=(("PUT /gate/profile", PERSONAL),)),
    Section("sign-in", "Sign-in", "account",
            "Single sign-on, last fresh sign-in, active sessions, sign out others",
            writes=(("PUT /gate/password", PERSONAL),)),
    Section("keys", "Keys", "account",
            "Your personal keys: create, label, expiry, rescope in place (same token), revoke",
            writes=(("POST /gate/keys", PERSONAL),
                    ("POST /gate/keys/mine/{key_id}/rescope", PERSONAL),
                    ("POST /gate/keys/mine/{key_id}/revoke", PERSONAL))),
    Section("notifications", "Notifications", "account",
            "Channels, quiet hours, what counts as Needs you",
            writes=(("notifications.recipient_set (self)", PERSONAL),)),
    Section("preferences", "Preferences", "account",
            "Theme, landing, rail items, default scope",
            writes=(("flags.set scope=user (self)", PERSONAL), ("browser preference", PERSONAL))),
    Section("agents", "My agents", "account",
            "Delegations and agents you are accountable for: pause, resume"),
    Section("data", "Data", "account",
            "Export what you own; your own change log (settings.changes mine=true)"),
    Section("devices", "Devices", "account", "Enrolled devices and voice", read_only=True),
    Section("qualifications", "Qualifications", "account", "Your qualifications and when they expire",
            read_only=True),
)

SETTINGS: tuple[Section, ...] = (
    Section("flags", "Flags", "settings", "Node and account overrides of feature flags",
            roles=ADMIN_ROLES, writes=(("flags.set scope=node|account", SITE),)),
    Section("retention", "Data and retention", "settings",
            "Site retention (lengthen only) and export-control defaults",
            roles=ADMIN_ROLES, writes=(("settings.set retention.*", SITE),)),
    Section("health", "Health", "settings", "Disk margins, backups and services on this site",
            roles=ADMIN_ROLES, read_only=True),
    Section("change-log", "Change log", "settings",
            "Every settings, flag, role, key and membership change, newest first; the audit trail beneath",
            roles=ADMIN_ROLES, read_only=True),
    Section("system", "System", "settings", "Allowlisted node settings", roles=ADMIN_ROLES,
            writes=(("PATCH /api/v1/settings", SITE),)),
)


def sections_for(roles: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    """The Account and Settings sections this caller sees, in order."""
    held = set(roles or ())
    return {
        "account": [s.as_dict() for s in ACCOUNT if s.visible_to(held)],
        "settings": [s.as_dict() for s in SETTINGS if s.visible_to(held)],
    }


__all__ = ["ACCOUNT", "ADMIN_ROLES", "PERSONAL", "SETTINGS", "SITE", "Section", "sections_for"]
