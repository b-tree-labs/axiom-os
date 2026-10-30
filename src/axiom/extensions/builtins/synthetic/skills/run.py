# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``synthetic.run``: run one journey and return its result.

``ok`` means the skill ran; the verdict is ``value["status"]`` (pass, fail or
unproven). Callers that gate on it must require ``pass``.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..loader import JourneyError, load_journey, parse_journey
from ..runner import run_journey


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    try:
        if "journey" in params and isinstance(params["journey"], dict):
            journey = parse_journey(params["journey"])
        elif params.get("path"):
            journey = load_journey(params["path"])
        else:
            return SkillResult(
                ok=False,
                value=None,
                errors=["give `path` (a journey TOML file) or `journey` (a dict)"],
            )
    except JourneyError as exc:
        return SkillResult(ok=False, value=None, errors=[str(exc)])
    if params.get("base_url"):
        from dataclasses import replace

        journey = replace(journey, base_url=str(params["base_url"]))
    result = run_journey(journey, env=params.get("env"))
    return SkillResult(
        ok=True,
        value=result.to_dict(),
        actions_taken=[f"ran journey {journey.name!r} against {journey.base_url}: {result.status}"],
    )


__all__ = ["run"]
