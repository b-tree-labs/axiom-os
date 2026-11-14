# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tab values for ``<cli> log <log>``: the handles of the logs on this node (ADR-243).

Filled from ``attest.logbook_list``, the skill ``<cli> log list`` calls, so Tab offers exactly
the logs the list shows.
"""

from __future__ import annotations

import logging
from typing import Any

from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext


def collect(_ctx: Any) -> dict[str, list[str]]:
    """``{"log": [handle, ...]}``; nothing when the logs cannot be read."""
    from axiom.infra.paths import get_user_state_dir

    from .skills import bind_default

    reg = bind_default()
    ctx = SkillContext(registry=reg, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.log"))
    try:
        result = invoke_capability(reg, "attest.logbook_list", {}, ctx, surface=CLI_SURFACE)
    except Exception:  # noqa: BLE001 - Tab never fails
        return {}
    if not result.ok or not isinstance(result.value, dict):
        return {}
    return {"log": [b.get("handle") or b["id"] for b in result.value.get("logbooks") or []]}


__all__ = ["collect"]
