# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``lane.claim`` — take a lane for this checkout.

Defaults are DERIVED (see :mod:`..naming`) so the common case is
``lane claim`` with no arguments from inside a worktree. A registry you must
remember to feed will always be missing the session that did not, and that
session is the one that corrupts a database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import naming, ports
from ..registry import DEFAULT_DSN_VAR, UNMANAGED, Lane, LaneTaken, Registry
from . import lanes_path


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    root = Path(params.get("root") or Path.cwd()).resolve()
    checkout = naming.repo_root(root)
    if checkout is None:
        return SkillResult(ok=False, errors=[f"{root} is not inside a git checkout"])

    name = str(params.get("name") or naming.slug(checkout.name))
    prefix = str(params.get("prefix") or "axiom_lane")
    dsn_var = str(params.get("dsn_var") or DEFAULT_DSN_VAR)
    reg = Registry(params.get("registry") or lanes_path())

    existing = reg.get(name)
    if existing is not None and not params.get("replace"):
        return SkillResult(
            ok=False,
            errors=[
                f"lane {name!r} is held by {existing.owner or 'someone'} "
                f"on :{existing.front}/{existing.api}"
            ],
            value={"lane": existing.name, "held_by": existing.owner},
        )

    override = params.get("ports")
    try:
        front, api = ports.resolve(
            name,
            taken=reg.taken_ports(),
            override=tuple(override) if override else None,
            canonical=existing.ports if existing else None,
        )
    except ports.NoPortsFree as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    database = "" if dsn_var == UNMANAGED else naming.database_name(checkout.name, prefix=prefix)
    lane = Lane(
        name=name,
        front=front,
        api=api,
        database=database,
        owner=str(params.get("owner") or ""),
        branch=str(params.get("branch") or ""),
        root=str(checkout),
        dsn_var=dsn_var,
        trees=list(params.get("trees") or []),
        venvs=list(params.get("venvs") or []),
        note=str(params.get("note") or ""),
    )
    try:
        reg.claim(lane, replace=bool(params.get("replace")))
    except LaneTaken as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    exports = {"AXIOM_LOCAL_PORT": str(front), "AXIOM_API_PORT": str(api)}
    if lane.isolated:
        exports[dsn_var] = f"postgresql://localhost/{database}"

    return SkillResult(
        value={
            "lane": lane.name,
            "front": front,
            "api": api,
            "database": database or None,
            "dsn_var": dsn_var,
            "isolated": lane.isolated,
            "root": str(checkout),
            "exports": exports,
            "next": (
                [] if not lane.isolated else [f"createdb {database}", "axi db migrate upgrade head"]
            ),
        },
        actions_taken=[f"claimed lane {name} on :{front}/{api}"],
    )
