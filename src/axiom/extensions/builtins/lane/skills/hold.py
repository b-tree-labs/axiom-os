# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``lane.hold`` / ``lane.drop`` — say which shared files you are editing.

A lane already keeps two sessions off each other's ports and out of each
other's database. Neither is what actually went wrong. What went wrong was
one file edited from two sides at once, two ADRs given the same number, and
two components written under the same name — each found in review, after
both sides had been done twice.

Neither verb takes a lane name. The lane is the one this checkout holds,
derived the same way ``claim`` derives it, because a registry you have to
remember to address is a registry the busy session skips.

The contention comes back from ``hold`` itself rather than waiting for
doctor. Finding out at doctor time is finding out after the second copy
exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import naming
from ..registry import Registry
from . import lanes_path


def _lane_for(params: dict[str, Any]) -> tuple[str | None, str]:
    """The lane this checkout owns, or why it could not be worked out."""
    root = Path(params.get("root") or Path.cwd()).resolve()
    checkout = naming.repo_root(root)
    if checkout is None:
        return None, f"{root} is not inside a git checkout"
    return str(params.get("name") or naming.slug(checkout.name)), ""


def _paths(params: dict[str, Any]) -> list[str]:
    """Paths exactly as the caller gave them.

    Never resolved. Two sessions typing the same repo-relative path have to
    produce the same string; resolving against each caller's worktree would
    make one reservation look like two, which is the confusion this is here
    to end.
    """
    raw = params.get("paths") or params.get("path") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(p).strip() for p in raw if str(p).strip()]


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Take the named files for this lane, and report who else has them."""
    name, why = _lane_for(params)
    if name is None:
        return SkillResult(ok=False, errors=[why])
    paths = _paths(params)
    if not paths:
        return SkillResult(ok=False, errors=["name at least one path to hold"])

    reg = Registry(params.get("registry") or lanes_path())
    try:
        clash = reg.hold(name, paths)
    except KeyError as exc:
        return SkillResult(ok=False, errors=[str(exc).strip("'")])

    lines = [f"{name} holds {', '.join(paths)}"]
    # Taken either way — reported, not refused. Two sessions genuinely do
    # need the same file sometimes; what neither recovers from is not knowing.
    lines += [f"  ALSO HELD BY {', '.join(who)}: {path}" for path, who in sorted(clash.items())]
    return SkillResult(
        ok=True,
        value={"lane": name, "held": paths, "contested": clash},
        actions_taken=lines,
    )


def drop(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Let the named files go. Dropping one never held is not an error."""
    name, why = _lane_for(params)
    if name is None:
        return SkillResult(ok=False, errors=[why])
    paths = _paths(params)
    if not paths:
        return SkillResult(ok=False, errors=["name at least one path to drop"])

    reg = Registry(params.get("registry") or lanes_path())
    try:
        gone = reg.drop(name, paths)
    except KeyError as exc:
        return SkillResult(ok=False, errors=[str(exc).strip("'")])

    return SkillResult(
        ok=True,
        value={"lane": name, "dropped": gone},
        actions_taken=[f"{name} released {', '.join(gone)}" if gone else f"{name} held none of those"],
    )


__all__ = ["drop", "run"]
