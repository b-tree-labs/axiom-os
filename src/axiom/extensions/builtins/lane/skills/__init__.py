# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Lane skills. CLI verbs are thin wrappers over these (ADR-056)."""

from __future__ import annotations

import os
from pathlib import Path

#: Every skill takes it, none requires it (ADR-139).
CALLER_GOAL = {"caller_goal": "str — one sentence: what you are trying to do"}


def lanes_path() -> Path:
    """Where the registry lives. Overridable so tests never touch the real one."""
    return Path(os.getenv("AXIOM_LANES_FILE") or Path.home() / ".axi" / "local" / "lanes.json")


def register(registry) -> None:
    from axiom.infra.skills import SkillSpec

    from . import claim, doctor, hold, list_lanes, release

    specs = [
        (
            "lane.claim",
            claim.run,
            "Take a lane for this checkout: a port pair, a database, a record.",
            {
                "name": "str",
                "ports": "tuple[int,int]",
                "dsn_var": "str",
                "trees": "list[str]",
                "venvs": "list[str]",
                "owner": "str",
                "branch": "str",
                "replace": "bool",
                **CALLER_GOAL,
            },
            True,
        ),
        (
            "lane.list",
            list_lanes.run,
            "Every claimed lane, and whether it is up.",
            {**CALLER_GOAL},
            False,
        ),
        (
            "lane.release",
            release.run,
            "Give a lane back. Never drops a database unless asked.",
            {"name": "str", "drop_database": "bool", **CALLER_GOAL},
            True,
        ),
        (
            "lane.hold",
            hold.run,
            "Say which shared files this lane is editing, and who else has them.",
            {"paths": "list[str]", "name": "str", **CALLER_GOAL},
            True,
            True,
        ),
        (
            "lane.drop",
            hold.drop,
            "Release files this lane was holding.",
            {"paths": "list[str]", "name": "str", **CALLER_GOAL},
            True,
            True,
        ),
        (
            "lane.doctor",
            doctor.run,
            "What the registry says against what the machine is doing.",
            {"explain": "bool", **CALLER_GOAL},
            False,
        ),
    ]
    # `idempotent` is NOT the inverse of `side_effects`, and deriving it that
    # way got hold and drop wrong. They write — so they gate — and re-running
    # one changes nothing, which is what the orchestrator's recovery path
    # actually asks: it re-runs a stranded action only where the skill says
    # a second run is safe. `claim` is the opposite shape: it writes AND a
    # second run without `replace` raises rather than no-ops.
    for name, fn, description, inputs, side_effects, *rest in specs:
        idempotent = rest[0] if rest else not side_effects
        registry.register_skill(
            SkillSpec(
                name=name,
                fn=fn,
                description=description,
                inputs=inputs,
                idempotent=idempotent,
                side_effects=side_effects,
                surfaces=("cli", "mcp", "agent_tool"),
            )
        )


__all__ = ["CALLER_GOAL", "lanes_path", "register"]
