# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Synthetic skills: thin, registry-bound entry points (ADR-056)."""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from . import run

# A journey sends scripted requests to a live web surface and returns a verdict. It changes nothing
# here, but it does act on a running system (logins, page loads), so it is more than a read.
_SPECS = (
    SkillSpec(
        name="synthetic.run",
        fn=run.run,
        description="Run one journey over HTTP and return pass, fail or unproven per step",
        inputs={"path": "str?", "journey": "dict?", "base_url": "str?", "env": "dict?"},
        side_effects=True,
        surfaces=("cli", "skill_md"),
        risk="reversible-write",
        requires=(),
    ),
)


def bind(registry: SkillRegistry) -> None:
    for spec in _SPECS:
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=True)


def bind_default() -> SkillRegistry:
    """Bind into the process-local default registry; idempotent."""
    reg = default_registry()
    bind(reg)
    return reg


__all__ = ["bind", "bind_default", "run"]
