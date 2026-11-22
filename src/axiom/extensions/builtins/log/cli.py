# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``<cli> log platform``: Platform events, the machine's own log (ADR-243).

Usage:
    <cli> log platform tail [--follow] [--ec] [--level LEVEL] [--provider PROVIDER] [--n N]
    <cli> log platform verify [--table TABLE]
    <cli> log platform stats
    <cli> log platform backend
    <cli> log platform sinks
    <cli> log platform export [--since DAYS]

The noun ``log`` belongs to the attest extension (``attest/log_cli.py``), which hands
``platform ...`` here. Each verb is a thin wrapper over a ``log.*`` skill (ADR-056): this
module parses flags and prints, and holds no logic of its own.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Any

from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext, SkillResult

#: Seconds between polls of ``tail --follow``.
FOLLOW_POLL_S = 0.2


def _brand_cli() -> str:
    """The command the operator actually typed."""
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def _prog() -> str:
    return f"{_brand_cli()} log platform"


def _ctx() -> SkillContext:
    from axiom.infra.paths import get_user_state_dir

    from .skills import bind_default

    return SkillContext(
        registry=bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.log.platform"),
    )


def _call(skill: str, params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    ctx = ctx or _ctx()
    return invoke_capability(ctx.registry, skill, params, ctx, surface=CLI_SURFACE)


def _failed(verb: str, result: SkillResult) -> int:
    for e in result.errors or ["failed"]:
        print(f"{_prog()} {verb}: {e}", file=sys.stderr)
    return 1


def _fmt(record: dict) -> str:
    from .skills.platform import format_record

    return format_record(record)


def _cmd_tail(args: argparse.Namespace) -> int:
    params = {"n": args.n, "ec": args.ec, "provider": args.provider, "level": args.level}
    ctx = _ctx()
    r = _call("log.tail", params, ctx)
    if not r.ok:
        return _failed("tail", r)
    for rec in r.value["records"]:
        print(_fmt(rec))
    if not args.follow:
        return 0
    end = r.value["end"]
    try:
        while True:
            r = _call("log.tail", {**params, "after": end}, ctx)
            if not r.ok:
                return _failed("tail", r)
            end = r.value["end"]
            for rec in r.value["records"]:
                print(_fmt(rec), flush=True)
            time.sleep(FOLLOW_POLL_S)
    except KeyboardInterrupt:
        print()
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    r = _call("log.verify", {"table": args.table})
    if not r.ok:
        return _failed("verify", r)
    if r.value.get("detail"):
        print(r.value["detail"])
        return 0
    if r.value["ok"]:
        print("OK")
        return 0
    at = r.value.get("broken_at")
    print("BROKEN" if at is None else f"BROKEN at record index {at}")
    return 1


def _cmd_stats(_args: argparse.Namespace) -> int:
    r = _call("log.stats", {})
    if not r.ok:
        return _failed("stats", r)
    v = r.value
    print(f"backend: {v['backend']}")
    if v.get("files") is None:
        print("\nJSONL files: (runtime/logs not found)")
    elif not v["files"]:
        print("\nJSONL files: (none found)")
    else:
        print("\nJSONL files:")
        for name, count in v["files"].items():
            print(f"  {name}: {count} lines")
    routing = v.get("routing_events")
    if routing is None:
        print("\nrouting_events: (file not found)")
    else:
        print(f"\nrouting_events:\n  ec_violations: {routing['ec_violations']}\n  blocked: {routing['blocked']}")
    return 0


def _cmd_backend(_args: argparse.Namespace) -> int:
    r = _call("log.backend", {})
    if not r.ok:
        return _failed("backend", r)
    print(r.value["name"])
    return 0


def _cmd_sinks(_args: argparse.Namespace) -> int:
    r = _call("log.sinks", {})
    if not r.ok:
        return _failed("sinks", r)
    print("\n".join(r.value["sinks"]) if r.value["sinks"] else "(no active log sinks)")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    r = _call("log.export", {"since": args.since})
    if not r.ok:
        return _failed("export", r)
    for rec in r.value["records"]:
        print(json.dumps(rec))
    return 0


#: ``log platform`` verb -> the skill it wraps (read by ``log_cli.SKILL_FOR_VERB``).
SKILL_FOR_VERB = {
    "tail": "log.tail",
    "verify": "log.verify",
    "stats": "log.stats",
    "export": "log.export",
    "backend": "log.backend",
    "sinks": "log.sinks",
}


def add_verbs(subparsers: Any) -> None:
    """The Platform events verbs, on ``subparsers`` (``log platform``'s own, or a test's)."""
    tail_p = subparsers.add_parser("tail", help="print the latest Platform events, or follow them")
    tail_p.add_argument("--follow", "-f", action="store_true", help="keep printing new events until interrupted")
    tail_p.add_argument("--ec", action="store_true", help="only events with ec_violation or is_ec set")
    tail_p.add_argument("--level", metavar="LEVEL", help="lowest level: debug, info, warning, error, critical")
    tail_p.add_argument("--provider", metavar="PROVIDER", help="only events from this provider")
    tail_p.add_argument("--n", type=int, default=20, metavar="N", help="how many recent events (default 20)")
    tail_p.set_defaults(func=_cmd_tail)

    verify_p = subparsers.add_parser("verify", help="check the Platform events audit chain")
    verify_p.add_argument("--table", metavar="TABLE", default=None, help="one audit table (default all)")
    verify_p.add_argument("--since", type=int, metavar="DAYS", default=None, help=argparse.SUPPRESS)
    verify_p.set_defaults(func=_cmd_verify)

    subparsers.add_parser("stats", help="the backend, file line counts, EC and blocked counts").set_defaults(
        func=_cmd_stats
    )
    subparsers.add_parser("backend", help="the backend that keeps Platform events").set_defaults(func=_cmd_backend)
    subparsers.add_parser("sinks", help="the sinks Platform events are written to").set_defaults(func=_cmd_sinks)

    export_p = subparsers.add_parser("export", help="write Platform events as JSON Lines to stdout")
    export_p.add_argument("--since", type=int, metavar="DAYS", default=None, help="only the last N days")
    export_p.set_defaults(func=_cmd_export)


def get_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog or _prog(),
        description="Platform events: the machine's own log. Not signed; read only.",
    )
    sub = parser.add_subparsers(dest="subcommand", metavar="VERB")
    sub.required = True
    add_verbs(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    """``log platform <verb>``: called by ``attest/log_cli.py``."""
    parser = get_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return args.func(args)


__all__ = ["SKILL_FOR_VERB", "add_verbs", "get_parser", "main"]
