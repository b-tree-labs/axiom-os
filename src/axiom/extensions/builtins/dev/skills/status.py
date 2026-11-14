# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``dev.status`` — whether this checkout's node is up, and where."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import config, node, running, schema, web
from . import held_lane, resolve_lane, topology_running


def _topology_lines(topo: dict) -> list[str]:
    from axiom.infra.cli_format import Column, table

    nodes = []
    for n in topo["nodes"]:
        where = n.get("url") or f":{n['api']}"
        if n["kind"] == "edge" and n.get("read") is not None:
            where += f"  read {n['read']}, lag {n['lag']}"
        nodes.append((n["node"], n["kind"], n["state"], f"lane {n['lane']}", where))
    links = []
    for lk in topo["links"]:
        detail = {
            k: v for k, v in lk.items() if k not in ("link", "state") and v not in (None, [], "")
        }
        links.append((lk["link"], lk["state"], str(detail) if detail else ""))
    lines = [f"topology {topo['topology']}:"]
    if nodes:
        lines += table(nodes, [Column(), Column(), Column(), Column(), Column(wrap=True)])
    if links:
        lines += table(links, [Column(), Column(), Column(wrap=True)])
    return lines


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    topo = None
    if topology_running(params, ctx):
        from .. import graph

        topo = graph.status(params, ctx)
        if held_lane(params) is None:
            up = all(n["state"] == "up" for n in topo["nodes"])
            return SkillResult(
                value={"state": "up" if up else "degraded", "topology": topo},
                actions_taken=_topology_lines(topo),
            )
    lane, errors = resolve_lane(params)
    if errors:
        return SkillResult(ok=False, errors=errors)
    home = node.home_for(ctx.state_dir, lane["name"])
    record = node.read_record(home)
    try:
        cfg = config.load(Path(lane["root"]))
    except config.DevConfigError:
        cfg = config.DevConfig()
    wrec = web.read_record(home)
    web_up = bool(wrec and node.alive(int(wrec["pid"])) and node.answers(wrec["url"]))
    rec = schema.read_record(home) or {}
    base = {
        "lane": lane["name"],
        "front": lane["front"],
        "api": lane["api"],
        "web": {
            "declared": cfg.web is not None,
            "up": web_up,
            "url": wrec["url"] if wrec else None,
        },
        "db": {
            "name": lane["database"] or None,
            "isolated": lane["isolated"],
            "migrated_commit": rec.get("commit"),
            "migrated_heads": rec.get("extensions", {}),
        },
    }
    if record is None:
        if topo:  # only the topology runs here: its state is the answer
            up = all(n["state"] == "up" for n in topo["nodes"])
            return SkillResult(
                value={**base, "state": "up" if up else "degraded", "urls": {}},
                actions_taken=_topology_lines(topo),
            )
        return SkillResult(value={**base, "state": "down", "urls": {}}, actions_taken=["down"])
    alive = node.alive(int(record["pid"]))
    answering = alive and node.answers(record.get("ready_url") or record["url"])
    state = "up" if answering else ("not answering" if alive else "exited")
    front_url = base["web"]["url"] if web_up else None
    urls = {"node": record["url"], "front": front_url}
    shown = front_url or record["url"]
    value = {**base, **record, "state": state, "urls": urls, "url": shown}
    if state == "up":
        value["running"] = running.of_record(record, home, Path(lane["root"]))
    return SkillResult(
        value=value,
        actions_taken=[f"{state}: {shown}", *(_topology_lines(topo) if topo else [])],
    )
