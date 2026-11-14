# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Who may read a log, and who may sign which of its types (logbook unit W1-A9).

One rule, used by the log list and by every route that reads a log's records:

- A person may **sign** an entry type when they hold one of its roles at the site (the interim
  ``roles.toml``); platform administration roles confer nothing (ADR-142 rule 8).
- A person may **read** a log when it declares no ``readers``, or they hold one of its
  ``readers`` roles, or they may sign any of its types.
- A log a person may read but not sign is **read-only** for them. A log they may not read does
  not exist for them: it is absent from the list and its routes answer 404.
"""

from __future__ import annotations

from typing import Any

from .logbooks import PLATFORM_ADMIN_ROLES, Logbook


def held(roles: Any) -> frozenset[str]:
    return frozenset(roles or ()) - PLATFORM_ADMIN_ROLES


def can_sign_type(logbook: Logbook, type_id: str, roles: Any) -> bool:
    return bool(held(roles) & set(logbook.type(type_id).roles))


def can_sign(logbook: Logbook, roles: Any) -> bool:
    mine = held(roles)
    return any(mine & set(t.roles) for t in logbook.types)


def can_read(logbook: Logbook, roles: Any) -> bool:
    if not logbook.readers:
        return True
    return bool(held(roles) & set(logbook.readers)) or can_sign(logbook, roles)


__all__ = ["can_read", "can_sign", "can_sign_type", "held"]
