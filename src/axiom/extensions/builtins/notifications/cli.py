# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi notifications`` — HERALD CLI surface.

Per ADR-056: every verb is a thin argparse → ``params`` dict → skill
invocation. Logic lives in ``skills/*.py``.

Verb grammar per ``docs/working/cli-verb-grammar-audit-2026-05-30.md``:
imperative verbs only — ``send``, ``list``, ``channels``. SEC-1 verbs;
HERALD-2 adds ``read``, ``reply``, ``snooze``, ``mute``, ``preferences``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from axiom.infra.paths import get_user_state_dir
from axiom.infra.skills import SkillContext, SkillResult

from . import skills as notif_skills

_PROG = "axi notifications"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=_PROG,
        description="HERALD — multi-channel notifications + unified inbox.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="emit the SkillResult as JSON (stable for piping)",
    )
    sub = p.add_subparsers(dest="verb", required=True)

    # ---- send ----------------------------------------------------------
    snd = sub.add_parser("send", help="Dispatch a notification.")
    snd.add_argument("--actor", default="@cli:local",
                     help="originating principal (default @cli:local)")
    snd.add_argument("--recipient", required=True,
                     help="recipient principal (Matrix-style @name:context)")
    snd.add_argument("--summary", required=True,
                     help="short summary surfaced in the inbox")
    snd.add_argument("--body", help="optional longer body")
    snd.add_argument(
        "--classification",
        default="internal",
        choices=["public", "internal", "regulated", "controlled"],
    )
    snd.add_argument(
        "--priority",
        default="normal",
        choices=["low", "normal", "high", "urgent"],
    )
    snd.add_argument("--intent", default="notification.send")
    snd.add_argument("--dedup-key", dest="dedup_key")

    # ---- list ----------------------------------------------------------
    lst = sub.add_parser("list", help="List inbox rows for a recipient.")
    lst.add_argument("--recipient", default="@cli:local")
    lst.add_argument("--unread", dest="unread_only", action="store_true")
    lst.add_argument(
        "--max-classification",
        dest="max_classification",
        choices=["public", "internal", "regulated", "controlled"],
    )
    lst.add_argument("--limit", type=int, default=50)

    # ---- channels -------------------------------------------------------
    sub.add_parser("channels", help="List registered channel adapters.")


    # ---- recipient {set|show|list} -------------------------------------
    rcp = sub.add_parser(
        "recipient",
        help="Manage recipient preferences (fan-out across channels).",
    )
    rcp_sub = rcp.add_subparsers(dest="recipient_verb", required=True)

    rset = rcp_sub.add_parser(
        "set",
        help="Set a recipient's ordered channel list (replaces any existing).",
    )
    rset.add_argument("recipient", help="recipient handle, e.g. @bbooth")
    rset.add_argument(
        "channels",
        help=(
            "comma-separated channel=address pairs, e.g. "
            "'slack=#alerts,twilio-sms=+15125550100@urgent,"
            "email=ben@example.com,inbox'"
        ),
    )

    rshow = rcp_sub.add_parser("show", help="Show a recipient's profile.")
    rshow.add_argument("recipient")

    rcp_sub.add_parser("list", help="List all recipient profiles.")

    return p


def _args_to_params(args: argparse.Namespace) -> dict[str, Any]:
    return {
        k: v for k, v in vars(args).items()
        if k not in ("verb", "json") and v is not None
    }


def _build_ctx() -> SkillContext:
    return SkillContext(
        registry=notif_skills.bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.notifications"),
        user_prompt=None,
    )


def _emit(result: SkillResult, as_json: bool) -> int:
    if as_json:
        print(json.dumps({
            "ok": result.ok,
            "value": result.value,
            "errors": result.errors,
            "actions_taken": result.actions_taken,
        }, indent=2, default=str))
        return result.exit_code

    if not result.ok:
        for err in result.errors:
            print(f"ERROR: {err}", file=sys.stderr)
        # still print actions_taken on partial failure (e.g. denied send)
        for a in result.actions_taken:
            print(a)
        return result.exit_code

    value = result.value or {}
    resource = value.get("resource")

    if resource == "inbox":
        items = value.get("items") or []
        count = value.get("count", len(items))
        if not items:
            print(f"(no inbox rows for {value.get('recipient')})")
            return result.exit_code
        print(f"{count} item(s):")
        for it in items:
            flag = " " if it["read"] else "*"
            print(
                f" {flag} {it['created_at']}  "
                f"[{it['classification']:<10}] "
                f"[{it['priority']:<6}] "
                f"{it['summary']}"
            )
        return result.exit_code

    if resource == "recipient_profile":
        recipient = value.get("recipient")
        channels = value.get("channels") or []
        print(f"{recipient}: {len(channels)} channel(s)")
        for c in channels:
            print(
                f"  {c['channel']:<14} "
                f"address={c['address']!s:<30} "
                f"min_priority={c['min_priority']}"
            )
        return result.exit_code

    if resource == "recipient_profiles":
        items = value.get("items") or []
        print(f"{value.get('count', 0)} recipient profile(s):")
        for it in items:
            print(f"  {it['recipient']}  ({len(it['channels'])} channel(s))")
            for c in it["channels"]:
                print(
                    f"    {c['channel']:<14} "
                    f"address={c['address']!s:<30} "
                    f"min_priority={c['min_priority']}"
                )
        return result.exit_code

    if resource == "channels":
        items = value.get("items") or []
        print(f"{value.get('count', 0)} channel(s):")
        for it in items:
            print(
                f"  {it['name']:<12} "
                f"[{it['direction']:<13}] "
                f"ceiling={it['classification_ceiling']:<10} "
                f"sla_p95_ms={it['delivery_sla_p95_ms']}"
            )
        return result.exit_code

    # Default: a send-result style dict.
    if "outcome" in value:
        print(
            f"{value['outcome']}: "
            f"channel={value.get('channel_selected')}, "
            f"receipt_id={value.get('receipt_id')}, "
            f"correlation_id={value.get('correlation_id')}"
        )
        if value.get("error"):
            print(f"  error: {value['error']}", file=sys.stderr)
        return result.exit_code

    print(json.dumps(value, indent=2, default=str))
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    ctx = _build_ctx()
    params = _args_to_params(args)
    if args.verb == "recipient":
        skill = f"notifications.recipient_{args.recipient_verb}"
        params.pop("recipient_verb", None)
    else:
        skill = f"notifications.{args.verb}"
    try:
        result = ctx.registry.invoke(skill, params, ctx)
    except KeyError as exc:
        result = SkillResult(ok=False, errors=[f"unknown verb: {exc}"])
    return _emit(result, args.json)


# AEOS manifest alias.
cli = main


if __name__ == "__main__":
    raise SystemExit(main())
