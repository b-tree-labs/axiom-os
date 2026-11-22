# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``video.status``: every source's session state and counters, with no credential in any of it."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..connector import ConnectorNotFound, NotAVideoConnector
from ..status import all_status
from ._common import command, refuse, source_name, state_dir


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = source_name(params) or None
    try:
        rows = all_status(state_dir=state_dir(ctx), name=name)
    except (ConnectorNotFound, NotAVideoConnector) as exc:
        return refuse(str(exc), f"`{command()} list` shows the declared sources")
    from ..boot import service_status

    return SkillResult(value={"sources": rows, "service": service_status(state_dir(ctx))})


__all__ = ["run"]
