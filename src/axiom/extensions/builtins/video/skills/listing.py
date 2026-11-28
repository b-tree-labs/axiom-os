# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``video.list``: the declared sources, the registered source kinds, or the triggers. Reads only."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..sources import source_kinds
from ..status import all_status
from ._common import state_dir


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    if params.get("kinds"):
        return SkillResult(value={"kinds": [k.as_dict() for k in source_kinds()]})
    if params.get("triggers"):
        from ..triggers import catalog

        return SkillResult(value={"triggers": catalog(refresh=True).rows()})
    rows = all_status(state_dir=state_dir(ctx))
    keep = ("name", "kind", "health", "state", "started")
    return SkillResult(value={"sources": [{k: row[k] for k in keep} for row in rows]})


__all__ = ["run"]
