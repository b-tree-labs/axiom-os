# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Skill functions behind ``axi dev`` (ADR-056)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext

CALLER_GOAL = {"caller_goal": "str — one sentence: what you are trying to do"}


def open_vault(ctx: SkillContext):
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore

    return ForeignCredentialStore(Path(ctx.state_dir))


def resolve_lane(params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """This checkout's lane: the one it holds, or a new one with no database.

    A dev node needs a port nobody else's checkout is using, which is what a
    lane already guarantees. It does not take the lane's database: the node
    runs without one until somebody asks for it.
    """
    from axiom.extensions.builtins.lane import naming
    from axiom.extensions.builtins.lane.registry import UNMANAGED, Registry
    from axiom.extensions.builtins.lane.skills import claim, lanes_path

    root = Path(params.get("root") or Path.cwd()).resolve()
    checkout = naming.repo_root(root)
    if checkout is None:
        return {}, [f"{root} is not inside a git checkout"]
    name = naming.slug(checkout.name)
    held = Registry(params.get("registry") or lanes_path()).get(name)
    if held is not None and Path(held.root).resolve() == checkout.resolve():
        return {"name": held.name, "front": held.front, "root": str(checkout)}, []
    claimed = claim.run(
        {
            "root": str(checkout),
            "name": name,
            "dsn_var": UNMANAGED,
            "registry": params.get("registry"),
            "note": "dev node",
        },
        None,
    )
    if not claimed.ok:
        return {}, claimed.errors
    return {
        "name": claimed.value["lane"],
        "front": claimed.value["front"],
        "root": str(checkout),
    }, []


def register(registry) -> None:
    from axiom.infra.skills import SkillSpec

    from . import down, status, up

    specs = [
        (
            "dev.up",
            up.run,
            "Run this checkout's node and print the one URL to open.",
            {
                "email": "str",
                "sign_in": "str — a vault credential name, or 'none'",
                "with": "list[str]",
                "restart": "bool",
                "timeout": "float",
                **CALLER_GOAL,
            },
            True,
            True,
        ),
        ("dev.down", down.run, "Stop this checkout's node.", {**CALLER_GOAL}, True, True),
        (
            "dev.status",
            status.run,
            "Whether this checkout's node is up, and where.",
            {**CALLER_GOAL},
            False,
            True,
        ),
    ]
    for name, fn, description, inputs, side_effects, idempotent in specs:
        registry.register_skill(
            SkillSpec(
                name=name,
                fn=fn,
                description=description,
                inputs=inputs,
                idempotent=idempotent,
                side_effects=side_effects,
                surfaces=("cli",),
            )
        )


__all__ = ["CALLER_GOAL", "open_vault", "register", "resolve_lane"]
