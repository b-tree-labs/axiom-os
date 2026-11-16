# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``dev.down`` — stop this checkout's node. Keeps its accounts and log."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import node, web
from . import held_lane, resolve_lane, topology_running


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    nodes: list[str] = []
    if topology_running(params, ctx):
        from .. import graph

        nodes = graph.down(params, ctx) or []
        if held_lane(params) is None:
            return SkillResult(value={"topology": nodes}, actions_taken=nodes)
    lane, errors = resolve_lane(params)
    if errors:
        return SkillResult(ok=False, errors=errors)
    home = node.home_for(ctx.state_dir, lane["name"])
    # The web server first: it proxies to the node, so it should never outlive it.
    site = web.stop(home)
    out = node.stop(home)
    said = f"stopped pid {out['pid']}" if out["stopped"] else out["reason"]
    actions = [said]
    if site["stopped"]:
        actions.append(f"stopped web dev server pid {site['pid']}")
    return SkillResult(
        value={"lane": lane["name"], **out, "web": site, **({"topology": nodes} if nodes else {})},
        actions_taken=[*nodes, *actions],
    )
