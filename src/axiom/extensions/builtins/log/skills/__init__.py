# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Platform events skills (ADR-056, ADR-243): what ``<cli> log platform <verb>`` calls.

Read-only and CLI-only: prd-logging does not put Platform events on an agent surface.
"""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from . import platform

_CLI = ("cli",)

SPECS: tuple[SkillSpec, ...] = (
    SkillSpec(
        name="log.tail",
        fn=platform.tail,
        description="The latest Platform events (routing events), filtered by EC flag, provider or level.",
        inputs={
            "n": "int", "ec": "bool", "provider": "str | None", "level": "str | None",
            "after": "int | None",
        },  # fmt: skip
        surfaces=_CLI,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="log.verify",
        fn=platform.verify,
        description="Check the Platform events audit chain and say where it broke, if it did.",
        inputs={"table": "str | None"},
        surfaces=_CLI,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="log.stats",
        fn=platform.stats,
        description="The Platform events backend, its file line counts, and EC and blocked counts.",
        surfaces=_CLI,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="log.export",
        fn=platform.export,
        description="Every Platform event (routing events), or those of the last N days.",
        inputs={"since": "int | None"},
        surfaces=_CLI,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="log.backend",
        fn=platform.backend,
        description="The name of the backend that keeps Platform events.",
        surfaces=_CLI,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
    SkillSpec(
        name="log.sinks",
        fn=platform.sinks,
        description="The active sinks Platform events are written to.",
        surfaces=_CLI,
        side_effects=False,
        idempotent=True,
        risk="read",
    ),
)


def bind(registry: SkillRegistry) -> None:
    for spec in SPECS:
        if not registry.has(spec.name):
            registry.register_skill(spec)


def bind_default() -> SkillRegistry:
    """Bind into the process-default registry (what help, Tab and ``skills emit-md`` read)."""
    registry = default_registry()
    bind(registry)
    return registry


__all__ = ["SPECS", "bind", "bind_default"]
