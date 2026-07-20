# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi connector`` — top-level CLI for the cross-cutting connector primitive.

Per ADR-056: every verb is a thin argparse → ``params`` dict → skill
invocation. Logic lives in ``skills/*.py``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from axiom.infra.paths import get_user_state_dir
from axiom.infra.skills import SkillContext, SkillResult

from . import skills as connector_skills

_PROG = "axi connector"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=_PROG,
        description="Manage external connectors (add / status / reconnect).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="emit the SkillResult as JSON (stable for piping)",
    )
    sub = p.add_subparsers(dest="verb", required=True)

    # ---- add -----------------------------------------------------------
    cadd = sub.add_parser(
        "add",
        help="Interactive wizard: configure a new connector instance.",
    )
    cadd.add_argument(
        "vendor",
        help="Vendor name (slack | mattermost | teams | email | twilio-sms).",
    )
    cadd.add_argument(
        "--name", default="default",
        help="Sub-slug for this connector instance (e.g. workspace name).",
    )
    cadd.add_argument(
        "--no-test-send", dest="no_test_send", action="store_true",
        help="Skip the post-install test send (CI / fully-scripted setup).",
    )

    # ---- status --------------------------------------------------------
    cstatus = sub.add_parser(
        "status",
        help="Show last-known outcome per registered connector.",
    )
    cstatus.add_argument(
        "connector", nargs="?",
        help="Optional: filter to one connector by name (e.g. slack).",
    )

    # ---- reconnect -----------------------------------------------------
    creconnect = sub.add_parser(
        "reconnect",
        help="Surface connectors needing reconnect + suggested next action.",
    )
    creconnect.add_argument(
        "connector", nargs="?",
        help="Optional: target one connector by name.",
    )

    return p


def _args_to_params(args: argparse.Namespace) -> dict[str, Any]:
    return {
        k: v for k, v in vars(args).items()
        if k not in ("verb", "json") and v is not None
    }


def _build_ctx() -> SkillContext:
    return SkillContext(
        registry=connector_skills.bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.connector"),
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
        for a in result.actions_taken:
            print(a)
        return result.exit_code

    value = result.value or {}
    resource = value.get("resource")

    # connector.add — wizard outcome
    if resource == "connector":
        vendor = value.get("vendor")
        nm = value.get("name")
        ready = value.get("ready")
        receipt = value.get("test_send_receipt")
        if ready:
            tail = f" (test delivered as {receipt})" if receipt else ""
            print(f"OK {vendor}:{nm} ready{tail}")
        else:
            print(f"{vendor}:{nm} not ready")
        for a in result.actions_taken:
            print(f"  - {a}")
        return result.exit_code

    # status — table
    if "connectors" in value and "reconnect_pending_count" in value:
        connectors = value.get("connectors") or []
        pending = value.get("reconnect_pending") or []
        if not connectors:
            print("(no connector outcomes recorded yet)")
            return result.exit_code
        if pending:
            print(
                f"⚠  {len(pending)} connector(s) need reconnect: "
                + ", ".join(p["connector"] for p in pending)
            )
        print(f"{len(connectors)} connector(s):")
        for c in connectors:
            marker = "✓" if c["ok"] else ("⟳" if c["reconnect_required"] else "✗")
            extra = f" status={c['status_code']}" if c.get("status_code") else ""
            err = f"  ({c['error']})" if c.get("error") else ""
            print(
                f"  {marker} {c['connector']:<14} "
                f"observed={c['observed_at']}{extra}{err}"
            )
        return result.exit_code

    # status — single connector
    if value.get("found") is True and "outcome" in value:
        o = value["outcome"]
        marker = "✓" if o["ok"] else ("⟳" if o["reconnect_required"] else "✗")
        print(f"{marker} {o['connector']} (last observed {o['observed_at']})")
        if o.get("status_code"):
            print(f"   status_code: {o['status_code']}")
        if o.get("error"):
            print(f"   error: {o['error']}")
        if o.get("vendor_code"):
            print(f"   vendor_code: {o['vendor_code']}")
        return result.exit_code
    if value.get("found") is False:
        print(value.get("note") or "no record")
        return result.exit_code

    # reconnect — pending list
    if "reconnect_pending_count" in value and "pending" in value:
        pending = value["pending"]
        if not pending:
            print(value.get("note") or "nothing to reconnect")
            return result.exit_code
        print(f"{len(pending)} connector(s) need reconnect:")
        for row in pending:
            print(f"  ⟳ {row['connector']}")
            print(f"    last error: {row.get('last_error') or '(none)'}")
            print(f"    next:       {row['next_action']}")
        return result.exit_code

    # reconnect — single connector action
    if value.get("needs_reconnect") is True and "next_action" in value:
        print(f"⟳ {value['connector']}: reconnect required")
        if value.get("last_error"):
            print(f"  last error: {value['last_error']}")
        print(f"  next: {value['next_action']}")
        return result.exit_code
    if value.get("needs_reconnect") is False:
        print(value.get("note") or f"{value.get('connector', '?')} is healthy")
        return result.exit_code

    print(json.dumps(value, indent=2, default=str))
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    ctx = _build_ctx()
    params = _args_to_params(args)
    skill = f"connector.{args.verb}"
    try:
        result = ctx.registry.invoke(skill, params, ctx)
    except KeyError as exc:
        result = SkillResult(ok=False, errors=[f"unknown verb: {exc}"])
    return _emit(result, args.json)


# AEOS manifest alias.
cli = main


if __name__ == "__main__":
    raise SystemExit(main())
