# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi fleet`` — the fleet console CLI.

Per ADR-056 each verb is a thin wrapper over a skill function: flags →
params dict → ``SkillRegistry`` dispatch. No logic lives here.
"""

from __future__ import annotations

import argparse
import json
import logging

from axiom.infra.paths import get_user_state_dir
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext, SkillResult

from . import skills as fleet_skills

_PROG = "axi fleet"

_STATUS_ICONS = {
    "green": "✓",
    "unproven": "?",
    "stale": "⏳",
    "failed": "✗",
    "unknown": "∅",
}


def get_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=_PROG,
        description="Fleet console — effect-checked status over nodes that push reports out.",
    )
    sub = p.add_subparsers(dest="verb")

    status = sub.add_parser("status", help="per-node status with evidence (read-time evaluation)")
    status.add_argument(
        "--site", action="append", dest="sites", help="narrow to a site (repeatable)"
    )
    status.add_argument("--include-archived", action="store_true")
    status.add_argument("--json", action="store_true", help="emit the SkillResult as JSON")

    report = sub.add_parser("report", help="push this node's snapshot to the configured console")
    report.add_argument("--push-url", help="override AXIOM_FLEET_PUSH_URL")
    report.add_argument("--node-id", help="override AXIOM_FLEET_NODE_ID / hostname")
    report.add_argument("--json", action="store_true", help="emit the SkillResult as JSON")

    return p


def _build_ctx() -> SkillContext:
    return SkillContext(
        registry=fleet_skills.bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.fleet"),
    )


def _emit(result: SkillResult, as_json: bool) -> int:
    if as_json:
        print(
            json.dumps(
                {
                    "ok": result.ok,
                    "value": result.value,
                    "errors": result.errors,
                    "actions_taken": result.actions_taken,
                },
                indent=2,
                default=str,
            )
        )
        return 0 if result.ok else 1
    if result.ok and isinstance(result.value, dict) and "nodes" in result.value:
        nodes = result.value["nodes"]
        if not nodes:
            print("no nodes have pushed reports (enrollment is per-node opt-in)")
        for node in nodes:
            icon = _STATUS_ICONS.get(node["rollup"], "?")
            print(f"{icon} {node['node_id']}  [{node['site']}]  {node['rollup'].upper()}")
            for kind, entry in sorted(node["kinds"].items()):
                k_icon = _STATUS_ICONS.get(entry["status"], "?")
                print(f"    {k_icon} {kind}: {entry['evidence']}")
    elif result.ok and isinstance(result.value, dict):
        for action in result.actions_taken:
            print(action)
    for err in result.errors:
        print(f"error: {err}")
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    args = get_parser().parse_args(argv)
    if args.verb is None:
        get_parser().print_help()
        return 2
    ctx = _build_ctx()
    if args.verb == "status":
        params = {
            "sites": args.sites,
            "include_archived": args.include_archived,
        }
        result = invoke_capability(ctx.registry, "fleet.status", params, ctx, surface=CLI_SURFACE)
    else:
        params = {}
        if args.push_url:
            params["push_url"] = args.push_url
        if args.node_id:
            params["node_id"] = args.node_id
        result = invoke_capability(ctx.registry, "fleet.report", params, ctx, surface=CLI_SURFACE)
    return _emit(result, args.json)


__all__ = ["get_parser", "main"]
