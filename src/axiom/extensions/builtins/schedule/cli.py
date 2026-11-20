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
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
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

    # The agenda read (MVP-24): what is on, across sources, in site time.
    ag = sub.add_parser(
        "agenda",
        help="What is on: runs, obligations and expiries in a window (read-only).",
    )
    ag.add_argument("--days", type=int, default=None, help="window length (default 7)")
    ag.add_argument("--from", dest="from_", default=None,
                    help="window start: YYYY-MM-DD (site time) or an ISO instant; default today")
    ag.add_argument("--to", default=None, help="window end: a date (inclusive) or an ISO instant")
    ag.add_argument("--source", default=None, help="only this source (schedule, attest, vault)")
    ag.add_argument("--person", default=None, help="only items owned by or addressed to this principal")
    ag.add_argument("--format", dest="fmt", choices=["table", "json", "ics"], default="table",
                    help="table (default), json, or ics (an iCalendar export of the same read)")
    ag.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                    help="same as --format json")

    it = sub.add_parser("item", help="One agenda item.")
    it_sub = it.add_subparsers(dest="item_verb", required=True)
    its = it_sub.add_parser("show", help="Show one agenda item by id (read-only).")
    its.add_argument("id")
    its.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                     help="emit the result as JSON")

    return p


def _agenda_params(args: argparse.Namespace) -> dict[str, Any]:
    params = {"from": args.from_, "to": args.to, "days": args.days,
              "source": args.source, "person": args.person}
    return {k: v for k, v in params.items() if v not in (None, "")}


def _print_agenda(value: dict[str, Any]) -> None:
    window = value["window"]
    print(f"Agenda {window['from']} to {window['to']} -- times in {value['tz']} {value['tz_label']}")
    if not value["items"]:
        print("  nothing scheduled in this window")
    from axiom.infra.cli_format import Column, table

    rows = []
    for item in value["items"]:
        when = item["start"] if item["date_only"] else item["start"][:16].replace("T", " ")
        owner = f"  owner {item['owner']}" if item.get("owner") else ""
        link = (item.get("owner_link") or {}).get("cli")
        what = f"{item['title']}  [{item['source']}: {item['status']}]{owner}"
        rows.append((when, item["kind"], what))
        if link:
            rows.append(("", "", f"opens with: {link}"))
    for line in table(rows, [Column(), Column(), Column(wrap=True)]):
        print(line)
    for src in value.get("sources", []):
        if not src.get("ok"):
            print(f"  (source {src['name']} unavailable: {src.get('error')})")


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

    if args.verb in ("agenda", "item"):
        return _read_verb(args, registry, ctx)

    skill_name = f"schedule.{args.verb}"
    result = invoke_capability(registry, skill_name, _params(args), ctx, surface=CLI_SURFACE)

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


def _read_verb(args: argparse.Namespace, registry: Any, ctx: SkillContext) -> int:
    """``agenda`` and ``item show``: one skill call, rendered three ways."""
    if args.verb == "agenda":
        result = invoke_capability(registry, "schedule.items_list", _agenda_params(args), ctx,
                                   surface=CLI_SURFACE)
        fmt = "json" if getattr(args, "json", False) else args.fmt
    else:
        result = invoke_capability(registry, "schedule.items_show", {"id": args.id}, ctx,
                                   surface=CLI_SURFACE)
        fmt = "json" if getattr(args, "json", False) else "table"
    if not result.ok:
        for err in result.errors:
            print(f"error: {err}", file=sys.stderr)
        return result.exit_code or 1
    if fmt == "json":
        print(json.dumps(result.value, default=str))
    elif fmt == "ics":
        from .items import to_ics

        sys.stdout.write(to_ics(result.value))
    elif args.verb == "agenda":
        _print_agenda(result.value)
    else:
        item = result.value["item"]
        print(f"{item['title']}  ({item['kind']}, {item['source']}, {item['status']})")
        print(f"  id     {item['id']}")
        print(f"  when   {item['start']}{' (date)' if item['date_only'] else ''} -- {result.value['tz_label']}")
        if item.get("owner"):
            print(f"  owner  {item['owner']}")
        link = item.get("owner_link") or {}
        if link.get("cli"):
            print(f"  opens  {link['cli']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
