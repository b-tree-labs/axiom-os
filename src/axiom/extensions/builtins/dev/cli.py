# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi dev`` — thin argparse over the skills (ADR-056)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext, SkillResult


def _cli_name() -> str:
    try:
        from axiom.infra.branding import get_branding

        return (get_branding().cli_name or "axi").strip()
    except Exception:
        return "axi"


#: Seconds between polls of ``dev logs --follow``.
FOLLOW_POLL_S = 0.5


def _state_dir():
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=f"{_cli_name()} dev",
        description="run this checkout's node locally, with one command",
    )
    verbs = p.add_subparsers(dest="verb")
    up = verbs.add_parser("up", help="start this checkout's node and print its URL")
    up.add_argument("--email", help="the first account (default: git user.email)")
    up.add_argument(
        "--sign-in",
        dest="sign_in",
        help=(
            "vault credential to sign in with, or 'none' for password only "
            "(default: the one the vault holds, else the bound site's public client)"
        ),
    )
    up.add_argument(
        "--sign-in-client",
        dest="sign_in_client",
        nargs=2,
        metavar=("ISSUER_URL", "CLIENT_ID"),
        help="the site's public sign-in client (no secret): recorded in the vault and offered",
    )
    up.add_argument(
        "--with",
        dest="with",
        action="append",
        default=[],
        metavar="SRC",
        help="another source root to run from, ahead of the installed package (repeatable)",
    )
    up.add_argument(
        "--db-url",
        dest="db_url",
        help="the database the node uses (default: $AXIOM_DB_URL; refuses when neither is set)",
    )
    up.add_argument("--restart", action="store_true", help="stop a running node first")
    up.add_argument(
        "--kind", choices=("local", "site", "edge"), help="the kind of node (ADR-255; default site)"
    )
    up.add_argument("--topology", help="bring up every node of a topology, each on its own lane")
    up.add_argument("--node", help="run one node of the topology alone")
    up.add_argument("--home", help="an edge's home: a node of the running topology")
    up.add_argument("--home-url", dest="home_url", help="an edge's home on another machine")
    up.add_argument(
        "--home-key", dest="home_key", help="vault entry holding the key that home issued"
    )
    up.add_argument("--site", help="the site an edge collects for")
    up.add_argument("--timeout", type=float, help="seconds to wait for the app (default 90)")
    down = verbs.add_parser("down", help="stop this checkout's node")
    status = verbs.add_parser("status", help="whether this checkout's node is up")
    logs = verbs.add_parser("logs", help="this lane's node and web server logs; -f follows them")
    logs.add_argument("-f", "--follow", action="store_true", help="keep printing new lines until interrupted")
    logs.add_argument("--n", type=int, default=40, help="lines of each log to start with (default 40)")
    db = verbs.add_parser("db", help="this lane's own database: status, migrate, reset, seed")
    dbv = db.add_subparsers(dest="db_verb")
    for nm, hp in (
        ("status", "heads per extension against this checkout"),
        ("migrate", "create the database if absent and migrate it for this checkout"),
        ("reset", "drop and recreate this lane's own database (needs --yes)"),
        ("seed", "load the checkout's [dev].seed sample data"),
    ):
        sp = dbv.add_parser(nm, help=hp)
        sp.add_argument(
            "--db-url", dest="db_url", help="the database server (default: $AXIOM_DB_URL)"
        )
        sp.add_argument(
            "--json", dest="json_after", action="store_true", help="machine-readable output"
        )
        sp.add_argument("--caller-goal", dest="caller_goal", default="", help=argparse.SUPPRESS)
        if nm == "reset":
            sp.add_argument("--yes", action="store_true", help="confirm the drop")
            sp.add_argument("--database", help="must equal this lane's own database name")
    srv = dbv.add_parser("server", help="the one shared local Postgres: up, down, status")
    srvv = srv.add_subparsers(dest="server_verb")
    for nm, hp in (
        ("up", "start it (Docker)"),
        ("down", "stop it; data is kept"),
        ("status", "whether it is running"),
    ):
        sp = srvv.add_parser(nm, help=hp)
        sp.add_argument(
            "--json", dest="json_after", action="store_true", help="machine-readable output"
        )
        sp.add_argument("--caller-goal", dest="caller_goal", default="", help=argparse.SUPPRESS)
    check = verbs.add_parser(
        "check", help="prove hot reload: change a token, see it served within 3 s"
    )
    check.add_argument(
        "--db-url", dest="db_url", help="the database server (default: $AXIOM_DB_URL)"
    )
    check.add_argument("--email")
    check.add_argument("--sign-in", dest="sign_in")
    check.add_argument("--timeout", type=float)
    check.add_argument(
        "--topology", help="check a topology: readings arrive once with the home down mid-stream"
    )
    check.add_argument("--count", type=int, help="readings per edge (default 60)")
    fault = verbs.add_parser(
        "fault", help="inject a fault at an edge: dev fault <node> <fault> [value]"
    )
    fault.add_argument("node")
    fault.add_argument(
        "fault",
        choices=("source-offline", "home-down", "relay-down", "slow-link", "clock-skew", "clear"),
    )
    fault.add_argument("value", nargs="?", type=float, help="seconds, for slow-link and clock-skew")
    as_ = verbs.add_parser("as", help="sign in as a tenant's persona: dev as <tenant>[:persona]")
    as_.add_argument(
        "target", nargs="?", help="tenant[:persona] (default: the first tenant, site-admin)"
    )
    as_.add_argument("--open", action="store_true", help="open the tenant's URL in the browser")
    shot = verbs.add_parser("shot", help="write screenshots to ./.dev/shots/")
    for verb in (up, down, status, logs, check, shot, as_, fault):
        verb.add_argument(
            "--json", dest="json_after", action="store_true", help="machine-readable output"
        )
        verb.add_argument("--caller-goal", dest="caller_goal", default="", help=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    return p


def _render(verb: str, result: SkillResult) -> str:
    from .running import render as render_running

    v = result.value or {}
    if verb == "up" and v.get("topology"):
        lines = list(result.actions_taken)
        if v.get("url"):
            lines += [
                "",
                f"  {v['url']}",
                f"  status: {_cli_name()} dev status   stop: {_cli_name()} dev down",
            ]
        return "\n".join(lines) or "done"

    v = result.value or {}
    block = [f"  {line}" for line in render_running(v["running"])] if v.get("running") else []
    if verb != "up":
        head = "\n".join(result.actions_taken) or "done"
        return "\n".join([head, *block]) if block else head
    cli = _cli_name()
    # Every field is read with .get: a missing one must never turn a working node into a crash.
    email = v.get("email") or "your git user.email (set it, or pass --email)"
    lines = ["", *block, *([""] if block else []), f"  {v.get('url', '')}", ""]
    if (v.get("web") or {}).get("up") and v.get("node_url"):
        lines += [
            f"  node: {v['node_url']}  (lane {v.get('lane')}: front :{v.get('front')}, api :{v.get('api')})",
            "",
        ]
    if not block:
        # A result without the block (an older caller) still says how to sign in.
        if v.get("sign_in_public") and v.get("sign_in_host"):
            where = " (from the bound site)" if v.get("sign_in_source") == "bound site" else ""
            lines.append(f"  Sign in: SSO via {v['sign_in_host']}{where}")
            lines.append(f"  or with a password as {email}")
        elif v.get("sign_in"):
            lines.append(f"  Sign in: '{v['sign_in']}', or with a password as {email}")
        else:
            lines.append(f"  Sign in as {email}")
        if v.get("database"):
            # Always said, so a node pointed at the wrong database is visible at once.
            lines.append(f"  Database: {v['database']}")
    if v.get("password_secret"):
        lines.append(f"  Password: {cli} vault get {v['password_secret']} --reveal | pbcopy")
    if v.get("already_running"):
        # A request the running node is not answering is said first, not echoed (#1791).
        lines[0:0] = ["  already running", *(f"  {n}" for n in v.get("switch") or [])]
        lines += ["", f"  restart: {cli} dev up --restart", f"  stop: {cli} dev down"]
    else:
        lines += ["", f"  log: {v.get('log', '')}", f"  stop: {cli} dev down"]
    lines += [f"  {n}" for n in v.get("notes") or []]
    return "\n".join(lines)


def _logs(args: argparse.Namespace, registry, ctx: SkillContext) -> int:
    """``dev logs [-f]``: print each log's last lines; with ``-f``, poll ``dev.logs`` for more."""
    base = {"caller_goal": args.caller_goal} if args.caller_goal else {}
    result = invoke_capability(registry, "dev.logs", {**base, "n": args.n}, ctx, surface=CLI_SURFACE)
    if not result.ok:
        print("\n".join(result.errors), file=sys.stderr)
        return result.exit_code
    as_json = args.json or args.json_after
    if as_json:
        print(json.dumps(result.value, indent=2))
    else:
        for log in result.value["logs"]:
            print(f"==> {log['path']} <==")
            for line in log["lines"]:
                print(line)
    if not args.follow:
        return 0
    after = {log["name"]: log["end"] for log in result.value["logs"]}
    paths = {log["name"]: log["path"] for log in result.value["logs"]}
    last = None
    try:
        while True:
            time.sleep(FOLLOW_POLL_S)
            r = invoke_capability(registry, "dev.logs", {**base, "after": after}, ctx, surface=CLI_SURFACE)
            if not r.ok:
                print("\n".join(r.errors), file=sys.stderr)
                return r.exit_code
            for log in r.value["logs"]:
                after[log["name"]] = log["end"]
                if not log["lines"]:
                    continue
                if as_json:
                    print(json.dumps(log), flush=True)
                    continue
                if last != log["name"] and len(paths) > 1:
                    print(f"==> {log['path']} <==")
                last = log["name"]
                for line in log["lines"]:
                    print(line, flush=True)
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "verb", None) is None:
        parser.print_help()
        return 2
    from axiom.infra.skills import SkillRegistry

    from .skills import register

    registry = SkillRegistry()
    register(registry)
    ctx = SkillContext(
        registry=registry,
        state_dir=_state_dir(),
        logger=logging.getLogger("axi.dev"),
    )
    if args.verb == "logs":
        return _logs(args, registry, ctx)
    params = {
        k: val
        for k, val in vars(args).items()
        if k not in ("verb", "db_verb", "server_verb", "json", "json_after")
        and val not in (None, [], "", False)
    }
    if args.verb == "db" and not args.db_verb:
        parser.parse_args(["db", "--help"])
    if args.verb == "db" and args.db_verb == "server" and not args.server_verb:
        parser.parse_args(["db", "server", "--help"])
    if args.verb == "db" and args.db_verb == "server":
        name = f"dev.db_server_{args.server_verb}"
    else:
        name = f"dev.db_{args.db_verb}" if args.verb == "db" else f"dev.{args.verb}"
    try:
        result = invoke_capability(registry, name, params, ctx, surface=CLI_SURFACE)
    except KeyError:
        print(f"{name} is not a registered capability", file=sys.stderr)
        return 1
    if result.ok:
        print(
            json.dumps(result.value, indent=2)
            if (args.json or getattr(args, "json_after", False))
            else _render(args.verb, result)
        )
        if args.verb == "status" and result.value.get("state") not in ("up",):
            return 1
    else:
        print("\n".join(result.errors), file=sys.stderr)
    return result.exit_code
