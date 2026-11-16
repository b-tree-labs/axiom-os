# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``state.pin`` / ``state.unpin`` -- exempt a snapshot from pruning (or not)."""

from __future__ import annotations

from typing import Any

from axiom.infra import state_snapshots as ss
from axiom.infra.skills import SkillContext, SkillResult

from ._common import cli, resolve


def _set(params: dict[str, Any], ctx: SkillContext, pinned: bool) -> SkillResult:
    _sd, _root, _policy, store = resolve(params, ctx)
    try:
        sid = ss.resolve_id(store, params.get("id") or "latest")
        ss.set_pinned(store, sid, pinned)
    except ss.SnapshotError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(ok=True, value={"id": sid, "pinned": pinned,
                                       "next_steps": [f"{cli()} state ls"]},
                       actions_taken=[f"{'pinned' if pinned else 'unpinned'} {sid}"])


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    return _set(params, ctx, True)


def unpin(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    return _set(params, ctx, False)
