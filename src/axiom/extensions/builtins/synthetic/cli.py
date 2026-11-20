# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi synthetic``: run scripted user-journey checks.

Per ADR-056 each verb is a thin wrapper over a skill. Exit status is a gate:
0 only when every step passed; 1 when a step failed; 3 when nothing failed
but a step could not be run (unproven); 2 for usage errors.
"""

from __future__ import annotations

import argparse
import json
import logging

from axiom.infra.paths import get_user_state_dir
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext

from . import skills as synthetic_skills

_ICON = {"pass": "✓", "fail": "✗", "unproven": "?"}


def _brand_cli() -> str:
    """The command the operator actually typed (a consumer distribution renames it)."""
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def get_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=f"{_brand_cli()} synthetic",
        description="Scripted user-journey checks against a live web surface.",
    )
    sub = p.add_subparsers(dest="verb")
    run = sub.add_parser("run", help="run one journey; exit 0 only if every step passed")
    run.add_argument("journey", help="path to a journey TOML file")
    run.add_argument("--base-url", help="override the journey's target")
    run.add_argument("--json", action="store_true", help="emit the result as JSON")
    sm = sub.add_parser("smoke", help="run one role's declared smoke journeys; exit 0 only if every one passed")
    sm.add_argument("--manifest", required=True, help="the smoke manifest (per role, the journeys it must pass)")
    sm.add_argument("--role", required=True, help="the role whose journeys run")
    sm.add_argument("--base-url", required=True, help="the address under test (an idle slot, a front, a node)")
    sm.add_argument("--commit", default="", help="keep the verdict for this commit in the journey store")
    sm.add_argument("--json", action="store_true", help="emit the result as JSON")
    return p


def _build_ctx() -> SkillContext:
    return SkillContext(
        registry=synthetic_skills.bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.synthetic"),
    )


def main(argv: list[str] | None = None) -> int:
    args = get_parser().parse_args(argv)
    if args.verb not in ("run", "smoke"):
        get_parser().print_help()
        return 2
    ctx = _build_ctx()
    if args.verb == "smoke":
        return _smoke(args, ctx)
    params: dict = {"path": args.journey}
    if args.base_url:
        params["base_url"] = args.base_url
    result = invoke_capability(ctx.registry, "synthetic.run", params, ctx, surface=CLI_SURFACE)
    if not result.ok:
        for err in result.errors:
            print(f"error: {err}")
        return 2
    value = result.value
    if args.json:
        print(json.dumps(value, indent=2, default=str))
    else:
        print(
            f"{_ICON[value['status']]} {value['journey']}  {value['status'].upper()}  ({value['base_url']}, {value['duration_ms']} ms)"
        )
        for s in value["steps"]:
            print(f"    {_ICON[s['status']]} {s['name']}: {s['detail']}")
    return {"pass": 0, "fail": 1, "unproven": 3}[value["status"]]


def _smoke(args: argparse.Namespace, ctx: SkillContext) -> int:
    params = {"manifest": args.manifest, "role": args.role, "base_url": args.base_url}
    if args.commit:
        params["commit"] = args.commit
    result = invoke_capability(ctx.registry, "synthetic.smoke", params, ctx, surface=CLI_SURFACE)
    if not result.ok:
        for err in result.errors:
            print(f"error: {err}")
        return 2
    value = result.value
    if args.json:
        print(json.dumps(value, indent=2, default=str))
    else:
        print(f"{_ICON[value['status']]} smoke {value['role']}  {value['status'].upper()}  ({value['base_url']})")
        for j in value["journeys"]:
            print(f"    {_ICON[j['status']]} {j['journey']}")
            for s in j["steps"]:
                if s["status"] != "pass":
                    print(f"        {_ICON[s['status']]} {s['name']}: {s['detail']}")
        if value.get("recorded"):
            print(f"    recorded for {value['recorded']['commit'][:12]}")
    return {"pass": 0, "fail": 1, "unproven": 3}[value["status"]]


__all__ = ["get_parser", "main"]
