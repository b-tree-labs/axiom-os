# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.expectations`` — the catalogue of conforming expectations.

What a contributor reads before writing a filter into a normalizer: which
named, versioned checks the platform already runs on the way into silver, what
each takes, and which ones this node has declared and where. Reads files only;
writes nothing.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..conformance import expectations as ex

ACTIONS = ("list", "show", "declared")


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``action`` = list (default) | show (with ``name``) | declared."""
    action = str(params.get("action") or "list")
    if action not in ACTIONS:
        return SkillResult(ok=False, errors=[f"action must be one of {list(ACTIONS)}, got {action!r}"])

    if action == "list":
        return SkillResult(ok=True, value={"expectations": ex.catalogue()})

    if action == "show":
        name = str(params.get("name") or "")
        spec = ex.CATALOGUE.get(name)
        if spec is None:
            return SkillResult(
                ok=False,
                errors=[f"no expectation named {name!r}; the catalogue has {sorted(ex.CATALOGUE)}"],
            )
        return SkillResult(ok=True, value=spec.describe())

    declared, unreadable = ex.load_declared(ctx.state_dir)
    value = {
        "directory": str(ex.declarations_dir(ctx.state_dir)),
        "declared": [d.describe() for d in declared],
        "unreadable": unreadable,
    }
    # A file that is refused is a check somebody believes is running. Say so
    # as a failure, not a footnote.
    if unreadable:
        return SkillResult(
            ok=False,
            value=value,
            errors=[f"{path}: {why}" for path, why in sorted(unreadable.items())],
        )
    return SkillResult(ok=True, value=value)


__all__ = ["ACTIONS", "run"]
