# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Journal skills (ADR-056). Reads only; nothing writes on any surface (LB-J1-A7)."""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, SkillSpec

from .. import follow
from . import read

_READ = ("cli", "mcp", "agent_tool")

SPECS: tuple[SkillSpec, ...] = (
    SkillSpec(
        name="journal.query",
        fn=read.query,
        description=(
            "What happened on this node: journal rows the caller may see, newest first, "
            "filtered by lane, level, object id, subject and time; mine narrows to what the "
            "caller did or what was done for them, agent to one agent's activity. Each row "
            "references a record and links to it; none holds a copy."
        ),
        inputs={
            "lanes": "list[str] | str | None",
            "subjects": "list[str] | str | None",
            "subject": "str | None",
            "since": "str | None",
            "until": "str | None",
            "limit": "int",
            "cursor": "str | None",
            "mine": "bool",
            "agent": "str | None",
            "levels": "list[str] | str | None",
        },
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
        values={"lanes": "lane"},
    ),
    SkillSpec(
        name="journal.lanes",
        fn=read.lanes,
        description=(
            "The journal's lanes the caller may see: retention class and floor, whether a lane "
            "is rebuilt from a durable record, counts, hidden counts, and subjects no lane claims."
        ),
        inputs={},
        surfaces=_READ,
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
    ),
    SkillSpec(
        name="journal.verify",
        fn=read.verify,
        description=(
            "Check the journal's references against the records they point at (the logbook "
            "wins) and report the catch-up lag per source. Reports only; repairs nothing."
        ),
        inputs={"limit": "int"},
        surfaces=("cli",),
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
    ),
    SkillSpec(
        name="journal.tail",
        fn=follow.tail,
        description=(
            "Tail a lane: print its last rows in the Tail's columns (time, lane, actor, line, "
            "record number) and, with follow, every new row until interrupted. A lane the "
            'caller may not read prints "not available to you".'
        ),
        inputs={
            "lanes": "list[str] | str | None",
            "run": "str | None",
            "limit": "int",
            "follow": "bool",
            "mine": "bool",
            "agent": "str | None",
            "levels": "list[str] | str | None",
        },  # fmt: skip
        surfaces=("cli",),
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
        values={"lanes": "lane"},
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
