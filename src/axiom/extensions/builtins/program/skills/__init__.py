# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Program skills — invocable through the platform SkillRegistry (ADR-056).

Each skill is a plain function ``(params, ctx) -> SkillResult``, namespaced
under the extension's CLI noun: ``program.status``, ``program.render``,
``program.validate``. The CLI verbs are 1:1 thin wrappers; any agent or
harness reaches the identical surface through the registry.

Phase 1 of prd-program ships the read tool (R1 partial), the renderer, and
the data-file validator over the one-file-per-program contract (R2). The
remaining R1 verbs (collect, draft, post, priorities, attach) arrive with
the coordinator phases.
"""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from . import render, status, validate

_NAMESPACE = "program"

#: verb → (function, description, inputs, side_effects)
_SKILLS = {
    "status": (
        status.run,
        "One parameterized read over the program data file: items with "
        "owner, dates, proposed-or-committed status, percent, and links, "
        "scoped by person, lane, item, schedule, or priorities. Answers "
        "only from the data file — absent is reported as absent, never "
        "invented.",
        {"scope": "str!", "key": "str", "fmt": "str", "data": "Path"},
        False,
    ),
    "render": (
        render.run,
        "Render the program data file to one static HTML status page in an "
        "output directory. Built-in template, stdlib only; every item id "
        "and every link in the file is present on the page.",
        {"data": "Path", "out": "Path"},
        True,
    ),
    "validate": (
        validate.run,
        "Check a program data file against the axiom.program/0.1 schema "
        "and report every defect at once, or the file's shape when it is "
        "valid.",
        {"data": "Path"},
        False,
    ),
}


def bind(registry: SkillRegistry) -> None:
    """Register every program skill into ``registry``, with its spec."""
    for verb, (fn, description, inputs, side_effects) in _SKILLS.items():
        name = f"{_NAMESPACE}.{verb}"
        if registry.has(name):
            continue
        registry.register_skill(
            SkillSpec(
                name=name,
                fn=fn,
                description=description,
                inputs=inputs,
                side_effects=side_effects,
                idempotent=True,
                surfaces=("cli",),
            )
        )


def bind_default() -> SkillRegistry:
    """Bind into the process-local default registry; idempotent."""
    registry = default_registry()
    bind(registry)
    return registry


def verbs() -> list[str]:
    """Verb names without the namespace prefix. Used by the CLI parser."""
    return list(_SKILLS)


__all__ = ["bind", "bind_default", "verbs"]
