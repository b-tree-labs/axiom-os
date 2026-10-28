# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Who may see and use the administrative chat commands (ADR-168).

An administrative command (turning off a turn trace, say) is hidden from everyone else: it does not appear
in help or completion, and typing it behaves exactly like any unknown command. Hiding is a courtesy; the
control is that dispatch refuses it. The registry asks this module once per call.

Who is an admin:
  * if the ``chat.admin_principals`` setting lists principals, exactly those principals;
  * if it lists none, the person at their own terminal (the line REPL or the full-screen TUI) is, because
    they own the process and the machine; a served session (web, headless) never is.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

ADMIN_PRINCIPALS_SETTING = "chat.admin_principals"
_OWN_TERMINAL = frozenset({"repl", "tui"})


def _configured_admins() -> list[str]:
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore

        raw = SettingsStore().get(ADMIN_PRINCIPALS_SETTING, [])
    except Exception as exc:  # noqa: BLE001 - no settings means no configured list
        log.debug("admin principals setting unavailable: %s", exc)
        return []
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    return [str(p).strip() for p in (raw or []) if str(p).strip()]


def is_admin(agent: Any, *, surface: str = "repl") -> bool:
    session = getattr(agent, "session", None)
    if session is None:
        return False
    configured = _configured_admins()
    if configured:
        return (getattr(session, "principal_id", "") or "") in configured
    return surface in _OWN_TERMINAL
