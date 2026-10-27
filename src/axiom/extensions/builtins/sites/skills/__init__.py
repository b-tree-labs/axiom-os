# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The ``sites`` skills, declared once; every surface routes through the registry."""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, SkillSpec

from . import checklist as _skill

SPECS = (
    SkillSpec(
        name="sites.checklist",
        fn=_skill.run,
        description=(
            "Every ADR-178 readiness check for a site: ok, waiting, failing (with the fix) "
            "or not applicable. Reads only."
        ),
        inputs={
            "site": "the site to check",
            "min_version": "the oldest collector version the intake accepts",
            "stopped_after_h": "hours without a reading before data counts as stopped (default 24)",
        },
        surfaces=("cli", "mcp", "agent_tool"),
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
    ),
)


def bind(registry: SkillRegistry) -> None:
    for spec in SPECS:
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=False)


def bind_default() -> SkillRegistry:
    registry = SkillRegistry()
    bind(registry)
    return registry


__all__ = ["SPECS", "bind", "bind_default"]
