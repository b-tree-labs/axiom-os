# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi directory`` — thin dispatcher over the directory skills (ADR-056).

``axi directory sync [--dry-run] [--group G …] [--json] [--heartbeat]``
``axi directory status [--json]``

``--heartbeat`` is what the Background Service fires (see ``[agent]`` in the
manifest): same run, terse one-line output, exit 2 when any group failed so
``axi agents logs directory`` surfaces it.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Optional

from axiom.extensions.builtins.directory import skills as directory_skills
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability


def _context():
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillContext

    return SkillContext(
        registry=directory_skills.bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.directory"),
        user_prompt=None,
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="axi directory",
        description="Group and membership resolution — sync groups into the authorization store.",
    )
    sub = ap.add_subparsers(dest="verb", required=True)
    s = sub.add_parser("sync", help="project configured groups into the tuple store")
    s.add_argument("--dry-run", action="store_true", help="report the diff, write nothing")
    s.add_argument(
        "--group",
        action="append",
        default=[],
        metavar="ID",
        help="sync only this group (repeatable)",
    )
    s.add_argument(
        "--heartbeat",
        action="store_true",
        help="daemon mode: one-line summary, exit 2 on any failure",
    )
    s.add_argument("--json", action="store_true", help="emit the full report as JSON")
    st = sub.add_parser("status", help="configuration + last run (no network)")
    st.add_argument("--json", action="store_true")
    return ap


def _print_sync(result, *, as_json: bool, heartbeat: bool) -> None:
    v = result.value or {}
    if as_json:
        print(json.dumps(v, indent=2, sort_keys=True))
        return
    if not v.get("groups") and not v.get("errors"):
        print("directory sync: nothing configured")
    mode = "dry-run" if v.get("dry_run") else "sync"
    for g in v.get("groups", []):
        if g.get("error"):
            print(f"  {g['group_id']}: FAILED — {g['error']}")
        else:
            print(
                f"  {g['group_id']}: +{len(g['added'])} -{len(g['removed'])} ={len(g['unchanged'])}"
            )
    line = (
        f"directory {mode}: provider={v.get('provider')} groups={len(v.get('groups', []))} "
        f"added={v.get('added', 0)} removed={v.get('removed', 0)} ok={v.get('ok')}"
    )
    print(line if not heartbeat else line, file=sys.stdout)
    for err in result.errors:
        print(f"error: {err}", file=sys.stderr)


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    ctx = _context()
    if args.verb == "sync":
        params = {"dry_run": args.dry_run, "groups": args.group}
        result = invoke_capability(ctx.registry, "directory.sync", params, ctx, surface=CLI_SURFACE)
        _print_sync(result, as_json=args.json, heartbeat=args.heartbeat)
        if args.heartbeat:
            return 0 if result.ok else 2
        return result.exit_code
    result = invoke_capability(ctx.registry, "directory.status", {}, ctx, surface=CLI_SURFACE)
    print(json.dumps(result.value, indent=2, sort_keys=True))
    for err in result.errors:
        print(f"error: {err}", file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
