# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``connector.reload`` skill: apply changed storage connector configuration (ADR-220 §3).

ADR-056 thin wrapper behind ``axi connector reload [<name>]``. Two effects:

1. this process re-reads the configured connectors, builds each changed one
   beside the old, probes it and swaps only on a pass, and reports the health
   of every connector (``ready | degraded | failed | unmounted``);
2. it touches the reload stamp, so every long-running service on this host
   re-reads its configuration the next time it uses a connector. No service
   restarts.

``ok`` is False when any connector named in the result is ``failed`` (a
``degraded`` one is still serving its previous configuration, and says why).
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Params: ``connector`` (optional, one name); ``manager`` (tests); ``no_stamp`` (tests)."""
    from axiom.extensions.builtins.connector.storage.manager import (
        ConnectorHealth,
        StorageConnectorManager,
        touch_reload_stamp,
    )

    manager = params.get("manager") or StorageConnectorManager()
    name = params.get("connector") or None
    statuses = manager.reload(name)
    actions = []
    if not params.get("no_stamp"):
        stamp = touch_reload_stamp(getattr(ctx, "state_dir", None))
        actions.append(f"asked running services to reload (touched {stamp})")
    rows = [s.as_dict() for s in statuses.values()]
    failed = [s.name for s in statuses.values() if s.health is ConnectorHealth.FAILED]
    errors = [f"{s.name}: {s.reason}" for s in statuses.values() if s.name in failed]
    if name and not statuses:
        errors.append(f"no storage connector named {name!r} is configured")
    return SkillResult(
        ok=not errors,
        value={
            "resource": "storage_connectors",
            "connectors": rows,
            "roles": [r.as_dict() for r in manager.role_status().values()],
        },
        errors=errors,
        actions_taken=actions,
    )
