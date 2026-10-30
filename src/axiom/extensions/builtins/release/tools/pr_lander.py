# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Read-only backlog report. This tool never proposes, merges, or posts."""

from __future__ import annotations

import logging
from typing import Any

from axiom.extensions.builtins.release.lander.skill import land
from axiom.infra.paths import get_user_state_dir
from axiom.infra.skills import SkillContext, SkillRegistry


def report(params: dict[str, Any] | None = None, ctx: SkillContext | None = None) -> dict:
    """Classify a backlog. ``propose`` is ignored: the tool only reports."""
    incoming = dict(params or {})
    incoming.pop("propose", None)
    if ctx is None:
        ctx = SkillContext(
            registry=SkillRegistry(),
            state_dir=get_user_state_dir(),
            logger=logging.getLogger("release.land"),
        )
    result = land(incoming, ctx)
    if not result.ok:
        raise RuntimeError("; ".join(result.errors) or "lander report failed")
    return result.value
