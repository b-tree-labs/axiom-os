# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi audit`` — GUARD audit-trail CLI.

Per ADR-056: every verb is a thin wrapper over a skill function. The
skills live in ``authz/skills/``; this file only translates argparse
namespaces into ``params`` dicts and renders the ``SkillResult``.

Verbs shipped in AUTHZ-1: ``list``, ``show``. AUTHZ-2 adds ``chain``
/ ``causes`` / ``graduation``; AUTHZ-3 adds ``explain``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from axiom.infra.paths import get_user_state_dir
from axiom.infra.skills import SkillContext, SkillResult

from . import skills as audit_skills

_PROG = "axi audit"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=_PROG,
        description="GUARD audit — query authorization verdicts + provenance.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="emit the SkillResult as JSON (stable for piping)",
    )
    sub = p.add_subparsers(dest="verb", required=True)

    # ---- list -----------------------------------------------------------
    lst = sub.add_parser(
        "list",
        help="List recent verdicts. Filters AND-compose.",
    )
    lst.add_argument(
        "--since",
        default="7d",
        help="time window: Nm/Nh/Nd/Nw or ISO-8601 (default: 7d)",
    )
    lst.add_argument(
        "--primitive",
        help="match leading segment of intent (e.g. 'notification')",
    )
    lst.add_argument(
        "--actor",
        help="exact actor principal (e.g. '@jim:example-org')",
    )
    lst.add_argument(
        "--decision",
        choices=[
            "permit",
            "deny",
            "propose_to_human",
            "rate_limit",
            "expired_capability",
        ],
        help="filter by decision class",
    )
    lst.add_argument(
        "--federation-origin",
        dest="federation_origin",
        help="filter by inbound peer cohort id",
    )
    lst.add_argument(
        "--limit",
        type=int,
        default=50,
        help="cap rows returned (default 50, max 500)",
    )

    # ---- show -----------------------------------------------------------
    shw = sub.add_parser(
        "show",
        help="Fetch one verdict receipt by id.",
    )
    shw.add_argument("receipt_id", help="receipt fragment id (uuidv7)")

    # ---- chain ----------------------------------------------------------
    chn = sub.add_parser(
        "chain",
        help="Walk a verdict's provenance backwards to its root.",
    )
    chn.add_argument("receipt_id", help="starting verdict id")

    # ---- causes ---------------------------------------------------------
    cau = sub.add_parser(
        "causes",
        help="List verdicts caused by a given fragment id.",
    )
    cau.add_argument("fragment_id", help="upstream fragment id")
    cau.add_argument(
        "--limit", type=int, default=50,
        help="cap rows returned (default 50, max 500)",
    )

    # ---- graduation -----------------------------------------------------
    grd = sub.add_parser(
        "graduation",
        help="Show RACI graduation state per (actor, intent_class, resource).",
    )
    grd.add_argument("--actor", help="filter to one actor principal")
    grd.add_argument("--intent-class", dest="intent_class",
                     help="filter to one intent class")
    grd.add_argument("--only-graduated", dest="only_graduated",
                     action="store_true",
                     help="only show actor/class pairs that have graduated")
    grd.add_argument("--only-proposing", dest="only_proposing",
                     action="store_true",
                     help="only show actor/class pairs still proposing")
    grd.add_argument("--limit", type=int, default=100,
                     help="cap rows (default 100, max 1000)")

    # ---- explain --------------------------------------------------------
    exp = sub.add_parser(
        "explain",
        help="Reconstruct the rationale for a verdict (load-bearing — covers every decision class).",
    )
    exp.add_argument("receipt_id", help="verdict id to explain")

    # ---- lint -----------------------------------------------------------
    lnt = sub.add_parser(
        "lint",
        help="no_action_without_authz static check (PRD §5.6).",
    )
    lnt.add_argument(
        "paths", nargs="*", default=["src/"],
        help="paths to check (default: src/)",
    )

    # ---- healthcheck ----------------------------------------------------
    sub.add_parser(
        "healthcheck",
        help="Probe GUARD readiness (mode + schema + decide()).",
    )

    return p


def _args_to_params(args: argparse.Namespace) -> dict[str, Any]:
    return {k: v for k, v in vars(args).items()
            if k not in ("verb", "json") and v is not None}


def _build_ctx() -> SkillContext:
    return SkillContext(
        registry=audit_skills.bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.audit"),
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
        return result.exit_code

    value = result.value or {}
    resource = value.get("resource")
    if resource == "verdicts":
        items = value.get("items") or []
        count = value.get("count", len(items))
        if not items:
            print("(no verdicts matched)")
            return result.exit_code
        print(f"{count} verdict(s):")
        for v in items:
            print(
                f"  {v['decided_at']}  "
                f"[{v['decision']:<18}] "
                f"{v['actor']:<22}  "
                f"{v['intent']:<32}  "
                f"{v['resource']}"
            )
        return result.exit_code

    if resource == "verdict":
        item = value.get("item") or {}
        for k, v in item.items():
            print(f"  {k}: {v}")
        return result.exit_code

    if resource == "chain":
        items = value.get("items") or []
        depth = value.get("depth", len(items))
        print(f"chain depth={depth} root_id={value.get('root_id')}"
              + (" (truncated)" if value.get("truncated") else ""))
        for v in items:
            if v.get("kind") == "external_root":
                print(f"  ↑ {v['id']:<40} (external root — non-verdict fragment)")
                continue
            print(
                f"  ↑ {v['id']:<40} {v['decided_at']}  "
                f"[{v['decision']:<18}] {v['actor']:<22}  {v['intent']}"
            )
        return result.exit_code

    if resource == "causes":
        items = value.get("items") or []
        count = value.get("count", len(items))
        print(f"{count} verdict(s) caused by fragment={value.get('fragment_id')}:")
        for v in items:
            print(
                f"  ↓ {v['decided_at']}  "
                f"[{v['decision']:<18}] {v['actor']:<22}  "
                f"{v['intent']:<32}  {v['resource']}"
            )
        return result.exit_code

    if resource == "lint":
        files = value.get("checked_files", 0)
        fns = value.get("checked_functions", 0)
        vs = value.get("violations") or []
        al = value.get("allowlisted") or []
        if vs:
            print(f"FAIL: {len(vs)} violation(s) "
                  f"({files} files, {fns} functions checked)")
            for v in vs:
                print(f"  {v['path']}:{v['lineno']}  {v['function']}: {v['reason']}")
        else:
            print(f"OK: 0 violations ({files} files, {fns} functions checked)")
        if al:
            print(f"  ({len(al)} allowlisted via # noqa)")
        return result.exit_code

    if resource == "explain":
        print(value.get("narrative", "(no narrative)"))
        print()
        trace = value.get("trace") or {}
        winner = trace.get("winning_rule")
        if winner:
            print(f"Winning rule:    {winner}")
        matched = trace.get("matched_rules") or []
        if matched:
            print(f"Matched rules:   {len(matched)}")
            for r in matched:
                print(
                    f"  • {r['name']}  "
                    f"disposition={r['disposition']}  priority={r['priority']}"
                )
        grad = trace.get("graduation")
        if grad:
            state = "graduated" if grad["graduated"] else "proposing"
            print(
                f"Graduation:      {state} ({grad['approvals']}/{grad['threshold']}) "
                f"intent_class={grad['intent_class']}"
            )
        if trace.get("federation_origin"):
            print(f"Federation:      inbound from {trace['federation_origin']}")
        return result.exit_code

    if resource == "graduation":
        items = value.get("items") or []
        count = value.get("count", len(items))
        if not items:
            print("(no graduation rows matched)")
            return result.exit_code
        print(f"{count} graduation row(s):")
        for g in items:
            state = "graduated" if g["graduated"] else "proposing"
            print(
                f"  [{state:<10}] {g['approvals']}/{g['threshold']}  "
                f"{g['actor']:<22}  {g['intent_class']:<28}  "
                f"{g['resource_pattern']}"
            )
        return result.exit_code

    if resource == "healthcheck":
        mode = value.get("mode")
        schema = value.get("schema") or {}
        dec = value.get("decide") or {}
        status = "OK" if result.ok else "FAIL"
        print(f"{status}  mode={mode}")
        print(f"  schema:  ok={schema.get('ok')}")
        for tbl, info in (schema.get("tables") or {}).items():
            mark = "✓" if info.get("ok") else "✗"
            extra = (f" rows={info.get('rows')}" if info.get("ok")
                     else f" error={info.get('error')}")
            print(f"    {mark} {tbl}{extra}")
        if "error" in schema:
            print(f"    error: {schema['error']}")
        print(
            f"  decide:  ok={dec.get('ok')}"
            + (f" decision={dec.get('decision')} next_action={dec.get('next_action')}"
               if dec.get('ok')
               else f" error={dec.get('error')}")
        )
        return result.exit_code

    print(json.dumps(value, indent=2, default=str))
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    ctx = _build_ctx()
    params = _args_to_params(args)
    try:
        result = ctx.registry.invoke(f"audit.{args.verb}", params, ctx)
    except KeyError as exc:
        result = SkillResult(ok=False, errors=[f"unknown verb: {exc}"])
    return _emit(result, args.json)


# Back-compat alias — the AEOS manifest entry was ``cli`` in the
# Phase-1 stub; keep it resolving so the dispatcher doesn't break
# without a manifest edit. Manifest can be updated to ``main`` in
# a follow-up.
cli = main


if __name__ == "__main__":
    raise SystemExit(main())
