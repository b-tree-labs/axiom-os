# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi graduation`` — inspect shadow-mode classifier outcome logs.

Per ADR-056 the CLI verb is a thin wrapper over a skill function: it
translates flags → a params dict and dispatches to ``graduation.status``
via the ``SkillRegistry``. No logic lives here.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime

from axiom.infra.paths import get_user_state_dir
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext, SkillResult

from . import skills as graduation_skills

_PROG = "axi graduation"


def get_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=_PROG,
        description="Inspect shadow-mode classifier outcome logs.",
    )
    sub = p.add_subparsers(dest="verb")
    status = sub.add_parser(
        "status", help="report the shadow outcome log for the tier classifier"
    )
    status.add_argument("--json", action="store_true",
                        help="emit the SkillResult as JSON")
    return p


def _build_ctx() -> SkillContext:
    return SkillContext(
        registry=graduation_skills.bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.graduation"),
    )


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _emit(result: SkillResult, as_json: bool) -> int:
    if as_json:
        print(json.dumps({
            "ok": result.ok,
            "value": result.value,
            "errors": result.errors,
            "actions_taken": result.actions_taken,
        }, indent=2, default=str))
        return 0 if result.ok else 1
    if result.ok and isinstance(result.value, dict):
        v = result.value
        print(f"switch:   {v['switch']} (phase {v['phase']})")
        print(f"enabled:  {v['enabled']}   backend: {v['backend']}")
        print(f"log:      {v['log_path']}")
        print(f"records:  {v['records']}   outcomes: {v['outcomes'] or '{}'}")
        print(f"labels:   {v['labels'] or '{}'}")
        print(f"first:    {_fmt_ts(v['first_timestamp'])}   last: {_fmt_ts(v['last_timestamp'])}")
    for err in result.errors:
        print(f"error: {err}")
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    args = get_parser().parse_args(argv)
    if args.verb is None:
        get_parser().print_help()
        return 2
    ctx = _build_ctx()
    result = invoke_capability(
        ctx.registry, "graduation.status", {}, ctx, surface=CLI_SURFACE
    )
    return _emit(result, args.json)


__all__ = ["get_parser", "main"]
