# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Sharing and access: Google Drive's roles on documents and folders, one decision point (ADR-263).

- :mod:`.roles`: the ladder (Owner > Editor > Commenter > Viewer; Invoker for agents later).
- :mod:`.verbs`: every verb and the least role it needs (prd-sharing-and-access SHR-R1.4).
- :mod:`.store`: the record in the ``authz`` schema (grants, settings, requests).
- :mod:`.decision`: ``role_of`` and ``require``, GUARD's ``access`` policy source.
- :mod:`.sharing`: the acts (share, remove, general access, settings, transfer, requests).

Every surface (CLI, MCP, chat, web) reaches these through its home's skills, so
each gets the same answer (DC-16).
"""

from axiom.governance.access.decision import (
    AccessError,
    AccessObject,
    Decision,
    NoAccess,
    Refused,
    Who,
    may,
    require,
    role_of,
    roles_of,
    settings_for,
    who,
)
from axiom.governance.access.roles import BOOK_MANAGER, DOCUMENT, FOLDER, RESTRICTED, SITE, Role
from axiom.governance.access.verbs import VERBS

__all__ = [
    "AccessError",
    "AccessObject",
    "BOOK_MANAGER",
    "DOCUMENT",
    "Decision",
    "FOLDER",
    "NoAccess",
    "RESTRICTED",
    "Refused",
    "Role",
    "SITE",
    "VERBS",
    "Who",
    "may",
    "require",
    "role_of",
    "roles_of",
    "settings_for",
    "who",
]
