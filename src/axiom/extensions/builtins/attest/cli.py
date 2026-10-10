# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi attest``: logbooks, signing and verification.

Per ADR-056 each verb is a thin wrapper over a skill: flags become params and
the registry dispatches. ``new`` and ``sign`` read the person's answer at the
terminal; with no terminal, nothing is signed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from axiom.infra.paths import get_user_state_dir
from axiom.infra.principal import resolve_principal
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext, SkillResult

from . import skills as attest_skills

_PROG = "axi attest"


def get_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=_PROG, description="Signed records of what people did, per logbook and site."
    )
    sub = p.add_subparsers(dest="verb")

    logbook = sub.add_parser("logbook", help="logbooks declared on this node")
    logbook_sub = logbook.add_subparsers(dest="logbook_verb")
    bl = logbook_sub.add_parser("list", help="list logbooks, entry types and signing roles")
    bl.add_argument("--json", action="store_true")
    bv = logbook_sub.add_parser("validate", help="validate a logbook file")
    bv.add_argument("path")
    bv.add_argument("--json", action="store_true")

    new = sub.add_parser("new", help="write an entry and sign it at the prompt")
    new.add_argument("--site")
    new.add_argument("--logbook", required=True)
    new.add_argument("--type", required=True, help="entry type, e.g. ROUND_CHECK")
    new.add_argument("--meaning", required=True, help="what signing means, e.g. performed")
    new.add_argument("--title", required=True)
    new.add_argument("--body")
    new.add_argument("--field", action="append", dest="fields", metavar="KEY=VALUE")
    new.add_argument("--json", action="store_true")

    sign = sub.add_parser("sign", help="complete and sign a draft proposed for you")
    sign.add_argument("draft_id")
    sign.add_argument("--field", action="append", dest="fields", metavar="KEY=VALUE")
    sign.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="show a record, or a logbook's latest records")
    show.add_argument("attestation_id", nargs="?")
    show.add_argument("--site")
    show.add_argument("--logbook")
    show.add_argument("--limit", type=int, default=20)
    show.add_argument("--json", action="store_true")

    verify = sub.add_parser("verify", help="verify a logbook's signed chain")
    verify.add_argument("--site")
    verify.add_argument("--logbook", required=True)
    verify.add_argument("--json", action="store_true")

    obl = sub.add_parser(
        "obligations", help="what is due, and whether it is ok, due soon or missed"
    )
    obl.add_argument("--site")
    obl.add_argument("--logbook")
    obl.add_argument("--json", action="store_true")

    anchor = sub.add_parser("anchor", help="sign a Merkle root over the site's logbook heads")
    anchor.add_argument("--site", help="one site; default every site with a chain")
    anchor.add_argument("--json", action="store_true")

    device = sub.add_parser("device", help="enrolled signing devices (administration)")
    device_sub = device.add_subparsers(dest="device_verb")
    de = device_sub.add_parser("enroll", help="enrol a device")
    de.add_argument("device_id")
    de.add_argument("--site")
    de.add_argument(
        "--class",
        dest="device_class",
        required=True,
        choices=["personal", "kiosk", "tablet", "phone"],
    )
    de.add_argument("--location")
    de.add_argument("--mobility", choices=["fixed", "portable"], default="fixed")
    de.add_argument("--json", action="store_true")
    dc = device_sub.add_parser("reclaim", help="issue a fresh one-time claim code")
    dc.add_argument("device_id")
    dc.add_argument("--json", action="store_true")
    dr = device_sub.add_parser("retire", help="retire a device")
    dr.add_argument("device_id")
    dr.add_argument("--json", action="store_true")
    dl = device_sub.add_parser("list", help="list enrolled devices")
    dl.add_argument("--site")
    dl.add_argument("--json", action="store_true")

    role = sub.add_parser("role", help="signing roles per site (administration)")
    role_sub = role.add_subparsers(dest="role_verb")
    for verb, text in (
        ("grant", "give a person a signing role"),
        ("revoke", "take a signing role away"),
    ):
        rp = role_sub.add_parser(verb, help=text)
        rp.add_argument("principal", help="@name:context")
        rp.add_argument("role", help="a role the logbook names, e.g. operator")
        rp.add_argument("--site")
        rp.add_argument("--json", action="store_true")
    rl = role_sub.add_parser("list", help="who holds which roles at a site")
    rl.add_argument("--site")
    rl.add_argument("--json", action="store_true")

    location = sub.add_parser("location", help="presence locations and their codes")
    location_sub = location.add_subparsers(dest="location_verb")
    li = location_sub.add_parser("init", help="create a location's secret in the vault")
    li.add_argument("location")
    li.add_argument("--site")
    li.add_argument("--json", action="store_true")
    lc = location_sub.add_parser("code", help="show the location's current code")
    lc.add_argument("location")
    lc.add_argument("--site")
    lc.add_argument("--json", action="store_true")

    export = sub.add_parser("export", help="write an evidence package for a logbook")
    export.add_argument("--site")
    export.add_argument("--logbook", required=True)
    export.add_argument("--out", required=True, help="a new or empty directory")
    export.add_argument("--json", action="store_true")
    return p


def _terminal_prompt(prompt: str) -> str:
    return input(prompt)


def _build_ctx() -> SkillContext:
    return SkillContext(
        registry=attest_skills.bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.attest"),
        user_prompt=_terminal_prompt if sys.stdin.isatty() else None,
        principal=resolve_principal(),
    )


def _print_text(result: SkillResult) -> None:
    v: Any = result.value
    if not isinstance(v, dict):
        return
    if "logbooks" in v:
        if not v["logbooks"]:
            print("no logbooks are declared on this node")
        for b in v["logbooks"]:
            print(f"{b['id']}  v{b['version']}  {b['display']}  (floor {b['posture']})")
            for t in b["types"]:
                print(f"  {t['id']}: {', '.join(t['meanings'])}; roles {', '.join(t['roles'])}")
    elif "records" in v:
        for r in v["records"]:
            print(
                f"#{r['seq']:>5}  {r['occurred_at']}  {r['entry_type'] or ''} "
                f"{r['meaning']}  {r['title'] or ''}  by {r['signer']}"
            )
    elif "record" in v:
        print(json.dumps(v["record"], indent=2, ensure_ascii=False))
    elif v.get("status") == "signed":
        print(f"signed {v['logbook']} #{v['seq']} at {v['site_id']}\n  {v['uri']}")
    elif v.get("status") in ("hold", "ask"):
        print(f"{v['status']}: nothing signed; draft {v['draft_id']} stays open")
    elif "anchors" in v:
        for a in v["anchors"]:
            print(f"anchored {a['site_id']}: {a['logbooks']} logbooks, root {a['root']}")
    elif "checked" in v:
        if v["ok"]:
            print(f"OK: {v['checked']} records; head #{v['head_seq']} {v['head_digest']}")
        else:
            print(
                f"BROKEN at #{v['first_bad_seq']}: {v['reason']} ({v['checked']} verified before it)"
            )
        a = v.get("anchor")
        if a is None:
            print("  no anchor covers this logbook yet")
        elif a["ok"]:
            print(f"  anchor {a['anchor_id']} ({a['created_at']}) matches")
        else:
            print(f"  anchor {a['anchor_id']} ({a['created_at']}) FAILS: {a['reason']}")
    elif "obligations" in v:
        if not v["obligations"]:
            print("nothing is owed: no interval with an obligation is open")
        for o in v["obligations"]:
            mark = {"ok": "✓", "warn": "⏳", "missed": "✕"}[o["state"]]
            print(
                f"{mark} {o['state'].upper():<6} {o['type']} in {o['interval']['kind']} "
                f"{o['interval']['number']}  due {o['due_at']}  ({o['severity']})"
            )
    elif "assignments" in v:
        if not v["assignments"]:
            print(f"nobody holds a signing role at {v['site_id']}")
        for a in v["assignments"]:
            print(f"{a['principal']}  {', '.join(a['roles'])}")
    elif "granted" in v:
        print(f"granted {v['granted']} to {v['principal']} at {v['site_id']}")
    elif "revoked" in v:
        print(f"revoked {v['revoked']} from {v['principal']} at {v['site_id']}")
    elif "devices" in v:
        if not v["devices"]:
            print("no devices are enrolled at this site")
        for d in v["devices"]:
            print(f"{d['device_id']}  {d['device_class']}  {d['location'] or '-'}  {d['mobility']}")
    elif "claim" in v and "device" not in v:
        c = v["claim"]
        print(f"claim code: {c['code']}  (one use, {c['expires_in_minutes']} min)\n  {c['how']}")
    elif "device" in v:
        d = v["device"]
        print(
            f"enrolled {d['device_id']} as {d['device_class']} at {d['location'] or 'no location'} ({d['mobility']})"
        )
        c = v.get("claim")
        if c:
            print(
                f"claim code: {c['code']}  (one use, {c['expires_in_minutes']} min)\n  {c['how']}"
            )
    elif "code" in v:
        print(f"{v['location']}: {v['code']}  (changes every {v['window_seconds']}s)")
    elif "out" in v:
        print(f"wrote {v['records']} records to {v['out']}\n  check with: {v['verify']}")
    elif "types" in v:
        print(f"valid: {v['id']} v{v['version']} ({len(v['types'])} types)")
        if v.get("not_yet_enforced"):
            print("  declared, not yet enforced: " + ", ".join(v["not_yet_enforced"]))


def _emit(result: SkillResult, as_json: bool) -> int:
    if as_json:
        print(
            json.dumps(
                {"ok": result.ok, "value": result.value, "errors": result.errors},
                indent=2,
                default=str,
                ensure_ascii=False,
            )
        )
    else:
        _print_text(result)
        for e in result.errors or []:
            print(f"error: {e}", file=sys.stderr)
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = get_parser()
    args = parser.parse_args(argv)
    nested = {
        "logbook": "logbook_verb",
        "device": "device_verb",
        "location": "location_verb",
        "role": "role_verb",
    }
    if args.verb is None or (args.verb in nested and getattr(args, nested[args.verb]) is None):
        parser.print_help()
        return 2
    if args.verb == "logbook":
        name = "attest.logbook_list" if args.logbook_verb == "list" else "attest.logbook_validate"
    elif args.verb in ("device", "location", "role"):
        name = f"attest.{args.verb}_{getattr(args, nested[args.verb])}"
    else:
        name = f"attest.{args.verb}"
    drop = {"verb", "json", *nested.values()}
    params = {k: v for k, v in vars(args).items() if k not in drop}
    ctx = _build_ctx()
    result = invoke_capability(ctx.registry, name, params, ctx, surface=CLI_SURFACE)
    return _emit(result, args.json)


__all__ = ["get_parser", "main"]
