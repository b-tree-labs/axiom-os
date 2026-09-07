# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Side effects a settings write must perform to stay truthful.

Most settings are inert — the value is read where it matters. ``autonomy.enabled``
is not, because it is enforced at *two* choke points: the runtime dispatch gate in
``background_service_main`` and the OS-timer write in ``register_all_daemon_agents``.

Turning autonomy **off** is idempotent: a surviving timer becomes a no-op within one
tick. Turning it back **on** was not, because nothing re-ran registration — so an
operator who declined at install and later opted in was left with a setting reading
``true``, no OS timer, no dispatch, and no explanation. This module closes that.

The import of the agents extension is deliberately soft: a settings write must never
fail because an extension is absent or broken.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

#: The one setting with a registration side effect.
AUTONOMY_KEY = "autonomy.enabled"


def _register_daemon_agents() -> list[Any]:
    """Indirection so tests can substitute the registration call."""
    from axiom.extensions.builtins.agents.cli import register_all_daemon_agents

    return register_all_daemon_agents()


def reconcile_autonomy_registration(enabled: bool) -> list[Any] | None:
    """Re-run OS-timer registration when autonomy is switched on.

    Returns the registration results, or ``None`` when nothing was done — either
    because autonomy was switched off (the runtime gate already handles that) or
    because the agents extension could not be reached.
    """
    if not enabled:
        # Off is enforced at runtime; removing timers here would fight the
        # operator who legitimately wants the service installed but quiet.
        return None
    try:
        return _register_daemon_agents()
    except Exception as exc:  # noqa: BLE001 — a settings write must still succeed
        log.warning("autonomy enabled, but agent registration could not run: %s", exc)
        return None


__all__ = ["AUTONOMY_KEY", "reconcile_autonomy_registration"]
