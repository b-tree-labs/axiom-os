# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``connector.reload`` skill: apply changed connector configuration (ADR-220 §3).

ADR-056 thin wrapper behind ``axi connector reload [<name>]``. Three effects:

1. this process re-reads the configured connectors, builds each changed one
   beside the old, probes it and swaps only on a pass, and reports the health
   of every connector (``ready | degraded | failed | unmounted``);
2. it touches the reload stamp, so every long-running service on this host
   re-reads its configuration the next time it uses a connector. No service
   restarts;
3. it asks every other connector family to apply its own configuration
   (``connector.configured.reload_configured``: a family's ``reload`` hook), so
   ``reload <name>`` works for any configured connector, not only storage.

``ok`` is False when any connector named in the result is ``failed`` (a
``degraded`` one is still serving its previous configuration, and says why), or
when a name was given and no family has it.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Params: ``connector`` (optional, one name); ``manager`` (tests); ``no_stamp`` (tests)."""
    from axiom.extensions.builtins.connector.configured import reload_configured
    from axiom.extensions.builtins.connector.storage.manager import (
        ConnectorHealth,
        StorageConnectorManager,
        touch_reload_stamp,
    )

    manager = params.get("manager") or StorageConnectorManager()
    name = params.get("connector") or None
    state_dir = getattr(ctx, "state_dir", None)
    if not state_dir:
        from axiom.infra.paths import get_user_state_dir

        state_dir = get_user_state_dir()
    configured, problems = reload_configured(state_dir, name)
    # Storage is asked about a name only if it has it (configured or mounted). It reports any
    # name it is asked about as unmounted, "no longer configured", so a typo, or another
    # family's connector, read as a storage connector that went away.
    asks_storage = name is None or name in (getattr(manager, "names", list)() or [])
    statuses = manager.reload(name) if asks_storage else {}
    actions = []
    if not params.get("no_stamp"):
        stamp = touch_reload_stamp(getattr(ctx, "state_dir", None))
        actions.append(f"asked running services to reload (touched {stamp})")
    rows = [s.as_dict() for s in statuses.values()]
    failed = [s.name for s in statuses.values() if s.health is ConnectorHealth.FAILED]
    errors = [f"{s.name}: {s.reason}" for s in statuses.values() if s.name in failed]
    errors += [
        f"{r['connector']}: {r.get('detail') or 'failed'}"
        for r in configured
        if r["health"] == "failed"
    ]
    if name and not statuses and not configured:
        errors.append(f"no connector named {name!r} is configured, in storage or any other family")
    return SkillResult(
        ok=not errors,
        value={
            "resource": "storage_connectors",
            "connectors": rows,
            "roles": [r.as_dict() for r in manager.role_status().values()] if asks_storage else [],
            "configured": configured,
            "configured_problems": problems,
        },
        errors=errors,
        actions_taken=actions,
    )
