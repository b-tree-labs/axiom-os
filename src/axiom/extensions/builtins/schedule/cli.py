# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi schedule`` — PULSE CLI.

Per ADR-056: CLI verbs are thin wrappers that translate flags → params
dict and dispatch to ``SkillRegistry.invoke``. All business logic
lives in the skill functions under ``schedule/skills/``.

Verb grammar: imperative, hyphenated (``fire-now``, not ``fire_now``).
See ``docs/working/cli-verb-grammar-audit-2026-05-30.md``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from axiom.infra.paths import get_user_state_dir
from axiom.infra.skills import SkillContext

from . import skills as schedule_skills

_PROG = "axi schedule"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=_PROG,
        description="PULSE — app-level domain scheduler.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="emit the SkillResult as JSON",
    )
    sub = p.add_subparsers(dest="verb", required=True)

    reg = sub.add_parser("register", help="Register a new schedule.")
    reg.add_argument("--cadence", required=True,
                     help='e.g. "interval:1h", "cron:0 */6 * * *", "one_shot"')
    reg.add_argument("--action", required=True, help="dotted callable ref")
    reg.add_argument("--description", default="")

    pa = sub.add_parser("pause", help="Pause an active schedule.")
    pa.add_argument("schedule_id")
    pa.add_argument("--reason", required=True)

    re = sub.add_parser("resume", help="Resume a paused schedule.")
    re.add_argument("schedule_id")

    ca = sub.add_parser("cancel", help="Cancel a schedule (terminal).")
    ca.add_argument("schedule_id")

    ls = sub.add_parser("list", help="List registered schedules.")
    ls.add_argument("--state", choices=["active", "paused", "cancelled"])

    fn = sub.add_parser("fire-now", help="Manual fire (authz-gated).")
    fn.add_argument("schedule_id")

    st = sub.add_parser("status", help="Detailed status for one schedule.")
    st.add_argument("schedule_id")

    return p


def _params(args: argparse.Namespace) -> dict[str, Any]:
    """Translate parsed argparse Namespace into a skill params dict."""
    d = vars(args).copy()
    d.pop("verb", None)
    d.pop("json", None)
    return d


def cli(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    registry = schedule_skills.bind_default()
    ctx = SkillContext(
        registry=registry,
        state_dir=get_user_state_dir() / "schedule",
        logger=logging.getLogger("axi.schedule"),
    )

    skill_name = f"schedule.{args.verb}"
    result = registry.invoke(skill_name, _params(args), ctx)

    if args.json:
        print(json.dumps({
            "ok": result.ok,
            "value": result.value,
            "errors": result.errors,
            "actions_taken": result.actions_taken,
        }, default=str))
    else:
        for action in result.actions_taken:
            print(action)
        for err in result.errors:
            print(f"error: {err}", file=sys.stderr)
        if result.ok and result.value is not None and not result.actions_taken:
            print(result.value)

    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(cli())
