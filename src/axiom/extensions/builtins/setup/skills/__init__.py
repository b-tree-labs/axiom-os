# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The ``setup`` skills (ADR-056): the walk, the read-only check, detection, and each step."""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, SkillSpec

from ..steps import builtin_steps
from . import run as _run
from .step import skill_for

_COMMON = {
    "node": "the node's base address (else the product's default, else the last run's)",
    "site": "the site the key is for, when the account may use more than one",
}

SPECS = [
    SkillSpec(
        name="setup.run",
        fn=_run.run,
        description=(
            "Get this computer ready: upgrade, start the vault, wire the shell, sign in to the "
            "node (key straight into the vault), connect AI apps. Asks before each change."
        ),
        inputs={**_COMMON, "yes": "answer yes to every change", "dry_run": "say what would change",
                "only": "list of step names to run"},
        surfaces=("cli",),
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        open_world=True,
        requires=(),
    ),
    SkillSpec(
        name="setup.check",
        fn=_run.check,
        description="Every setup check, read only: green, or red with the one thing to do.",
        inputs={**_COMMON, "only": "list of step names to check"},
        surfaces=("cli", "mcp", "agent_tool"),
        side_effects=False,
        idempotent=True,
        risk="read",
        open_world=True,
        requires=(),
    ),
    SkillSpec(
        name="setup.detect",
        fn=_run.detect,
        description="What this computer is: OS, shell and start-up file, installer, Docker, AI apps.",
        inputs=dict(_COMMON),
        surfaces=("cli", "mcp", "agent_tool"),
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
    ),
]

for _step in builtin_steps():
    SPECS.append(
        SkillSpec(
            name=f"setup.{_step.name}",
            fn=skill_for(_step),
            description=f"The '{_step.title}' setup step: action=check (read only), plan, or apply.",
            inputs={**_COMMON, "action": "check | plan | apply"},
            surfaces=("cli",),
            side_effects=True,
            idempotent=True,
            risk="reversible-write",
            requires=(),
        )
    )


def bind(registry: SkillRegistry) -> None:
    for spec in SPECS:
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=bool(spec.side_effects))


def bind_default() -> SkillRegistry:
    registry = SkillRegistry()
    bind(registry)
    return registry


__all__ = ["SPECS", "bind", "bind_default"]
