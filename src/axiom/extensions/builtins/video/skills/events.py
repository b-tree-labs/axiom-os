# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``video.events``: what the triggers saw, from the ledger. Reads only.

A source's history outlives its declaration, so a name nothing declares is
answered with its history (often none), never refused.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import ledger
from ._common import refuse, source_name, state_dir


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Params: ``name`` (optional, one source), ``since`` (optional ISO-8601 time)."""
    since = str(params.get("since") or "").strip() or None
    try:
        rows = ledger.read(state_dir(ctx), stream=source_name(params) or None, since=since)
    except ValueError as exc:
        return refuse(f"since: {exc}")
    return SkillResult(value={"events": rows})


__all__ = ["run"]
