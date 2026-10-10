# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi maintenance`` and ``axi support`` — thin argparse over the skills (ADR-056).

At the site:

    maintenance                      level, what is waiting, what ran (status)
    maintenance pending              requests waiting for a person here
    maintenance approve <id>         run a waiting request now
    maintenance deny <id>            refuse it; it never runs
    maintenance poll [--watch S]     fetch and act on requests (the node's service)
    support open --for 2h            open a recorded support session; Ctrl-C ends it
    support close                    end the session open on this machine

At the operator's desk:

    maintenance keygen <name>
    maintenance request <site> <action> [KEY=VALUE ...]
    maintenance results <site>
    support list <site>
    support attach <id>

Stopping all of it is a site decision: ``features maintenance off``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext


def _cli_name() -> str:
    try:
        from axiom.infra.branding import get_branding

        return (get_branding().cli_name or "axi").strip()
    except Exception:  # noqa: BLE001
        return "axi"


def _json_flag() -> argparse.ArgumentParser:
    """``--json`` accepted after the verb too; SUPPRESS keeps the subparser from resetting it."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
    return p


def _operator_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--relay-url", dest="relay_url")
    p.add_argument("--token-ref", dest="token_ref", help="vault reference to this operator's relay key")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"{_cli_name()} maintenance",
                                description="remote maintenance requests, which this site can see and stop")
    p.add_argument("--json", action="store_true", default=False)
    sub = p.add_subparsers(dest="verb")
    sub.add_parser("status", parents=[_json_flag()], help="level, waiting requests, recent results (the default)")
    sub.add_parser("pending", parents=[_json_flag()], help="requests waiting for a person at this site")
    for verb in ("approve", "deny"):
        v = sub.add_parser(verb, parents=[_json_flag()], help=f"{verb} a waiting request")
        v.add_argument("id")
        v.add_argument("--by", help="who is deciding (default: your login name)")
    au = sub.add_parser("apply-update", parents=[_json_flag()],
                        help="install a version a person approved, from the signed release channel")
    au.add_argument("--version", required=True)
    po = sub.add_parser("poll", parents=[_json_flag()], help="fetch this site's requests and act on them")
    po.add_argument("--watch", type=float, default=0, metavar="SECONDS", help="repeat every SECONDS")
    kg = sub.add_parser("keygen", parents=[_json_flag()], help="make an operator signing key (private half to the vault)")
    kg.add_argument("name")
    rq = sub.add_parser("request", parents=[_json_flag()], help="sign and send one request to a site")
    rq.add_argument("site")
    rq.add_argument("action")
    rq.add_argument("params", nargs="*", metavar="KEY=VALUE")
    rq.add_argument("--ttl", type=int, default=3600, help="seconds the request stays valid (max one day)")
    rq.add_argument("--key-id", dest="key_id")
    rq.add_argument("--operator")
    _operator_flags(rq)
    rs = sub.add_parser("results", parents=[_json_flag()], help="what a site's node reported")
    rs.add_argument("site")
    rs.add_argument("--after", type=int, default=0)
    _operator_flags(rs)
    return p


def build_support_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"{_cli_name()} support",
                                description="time-boxed, recorded support sessions a person at the site opens")
    p.add_argument("--json", action="store_true", default=False)
    sub = p.add_subparsers(dest="verb", required=True)
    op = sub.add_parser("open", parents=[_json_flag()], help="open a session from this machine (you can end it at any time)")
    op.add_argument("--for", dest="duration", required=True, help="how long: 30m, 2h (at most 8h)")
    op.add_argument("--reason", default="")
    sub.add_parser("close", parents=[_json_flag()], help="end the session open on this machine")
    ls = sub.add_parser("list", parents=[_json_flag()], help="(operator) sessions a site has open")
    ls.add_argument("site")
    _operator_flags(ls)
    at = sub.add_parser("attach", parents=[_json_flag()], help="(operator) join an open session; Ctrl-] leaves")
    at.add_argument("id")
    _operator_flags(at)
    return p


def _run(name: str, params: dict[str, Any], as_json: bool) -> int:
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillRegistry

    from .skills import bind

    registry = SkillRegistry()
    bind(registry)
    ctx = SkillContext(registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.maintenance"))
    result = invoke_capability(registry, name, {k: v for k, v in params.items() if v is not None}, ctx,
                               surface=CLI_SURFACE)
    if not result.ok:
        print("\n".join(result.errors), file=sys.stderr)
        return result.exit_code or 1
    emits_json = as_json or name not in ("maintenance.status", "maintenance.pending")
    for action in result.actions_taken:
        # Beside JSON on stdout, a human line goes to stderr so the JSON parses.
        print(f"• {action}", file=sys.stderr if emits_json else sys.stdout)
    if emits_json:
        print(json.dumps(result.value, indent=2, default=str))
    else:
        print(render(name, result.value))
    return 0


def render(name: str, value: dict[str, Any]) -> str:
    cli = _cli_name()
    if name == "maintenance.pending":
        rows = value.get("pending") or []
        if not rows:
            return "Nothing is waiting."
        return "\n".join(f"{r['id']}  {r['action']} {r['params'] or ''}  from {r['operator']}, "
                         f"valid until {r['expires_at']}" for r in rows) + (
            f"\n\nApprove: {cli} maintenance approve <id>   ·   deny: {cli} maintenance deny <id>")
    lines = [f"Remote maintenance: {value.get('level', 'off')} (site policy; "
             f"`{cli} features maintenance off` stops it)."]
    if not value.get("configured"):
        lines.append(value.get("problem") or "Not set up on this machine.")
        return "\n".join(lines)
    lines.append(f"Site {value['site']}, relay {value.get('relay_url') or '(none)'}; "
                 f"changes need approval: {value.get('approve')}.")
    lines.append(f"Trusted operator keys: {', '.join(value.get('trusted_keys') or []) or 'none'}.")
    lines.append(f"Wired actions: {', '.join(value.get('wired') or []) or 'none'}.")
    lines.append(f"Waiting for approval: {value.get('pending', 0)}.")
    for r in value.get("recent") or []:
        lines.append(f"  {r.get('finished_at') or '':20}  {r.get('action') or '?':16} {r['status']}"
                     + (f" ({r['reason']})" if r.get("reason") else ""))
    lines.append(f"Audit log: {value.get('audit_log')}")
    return "\n".join(lines)


def _kv(items: list[str]) -> dict[str, str]:
    out = {}
    for item in items:
        k, sep, v = item.partition("=")
        if not sep or not k:
            raise SystemExit(f"parameters are KEY=VALUE, got {item!r}")
        out[k] = v
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    verb = args.verb or "status"
    params: dict[str, Any] = {}
    if verb in ("approve", "deny"):
        params = {"id": args.id, "by": args.by}
    elif verb == "poll":
        params = {"watch_s": args.watch}
    elif verb == "apply-update":
        verb, params = "apply_update", {"version": args.version}
    elif verb == "keygen":
        params = {"name": args.name}
    elif verb == "request":
        params = {"site": args.site, "action": args.action, "params": _kv(args.params), "ttl_s": args.ttl,
                  "key_id": args.key_id, "operator": args.operator, "relay_url": args.relay_url,
                  "token_ref": args.token_ref}
    elif verb == "results":
        params = {"site": args.site, "after": args.after, "relay_url": args.relay_url, "token_ref": args.token_ref}
    return _run(f"maintenance.{verb}", params, getattr(args, "json", False))


def support_main(argv: list[str] | None = None) -> int:
    args = build_support_parser().parse_args(argv)
    params: dict[str, Any] = {}
    if args.verb == "open":
        params = {"for": args.duration, "reason": args.reason}
    elif args.verb == "list":
        params = {"site": args.site, "relay_url": args.relay_url, "token_ref": args.token_ref}
    elif args.verb == "attach":
        params = {"id": args.id, "relay_url": args.relay_url, "token_ref": args.token_ref}
    return _run(f"support.{args.verb}", params, getattr(args, "json", False))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "support":
        raise SystemExit(support_main(sys.argv[2:]))
    raise SystemExit(main())
