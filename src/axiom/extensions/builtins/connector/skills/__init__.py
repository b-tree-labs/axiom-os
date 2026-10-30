# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi connector`` skills — ADR-056 thin wrappers."""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from . import add, reconnect, reload, roles, status

_NAMESPACE = "connector"

_SKILLS = {
    "add": add.run,
    "status": status.run,
    "reconnect": reconnect.run,
}

#: Skills registered with a full spec (ADR-063 / ADR-185): new skills are clean by default.
_SPECS = (
    SkillSpec(
        name=f"{_NAMESPACE}.reload",
        fn=reload.run,
        description=(
            "Apply changed storage connector configuration without a restart: build each "
            "changed connector beside the old one, probe it, swap only on a pass, and report "
            "every connector's health (ready | degraded | failed | unmounted)."
        ),
        inputs={"connector": "one connector name (optional; default: every configured one)"},
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        requires=(),
        surfaces=("cli",),
    ),
    SkillSpec(
        name=f"{_NAMESPACE}.roles",
        fn=roles.run,
        description=(
            "Which connector serves each storage role: a configured one, the default "
            "store (and what that leaves missing), or absent (the feature is hidden)."
        ),
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
        surfaces=("cli",),
    ),
)


def bind(registry: SkillRegistry) -> None:
    for verb, fn in _SKILLS.items():
        name = f"{_NAMESPACE}.{verb}"
        if registry.has(name):
            continue
        registry.register(name, fn)
    for spec in _SPECS:
        if not registry.has(spec.name):
            registry.register_skill(spec)


def bind_default() -> SkillRegistry:
    reg = default_registry()
    bind(reg)
    return reg


__all__ = ["bind", "bind_default"]
