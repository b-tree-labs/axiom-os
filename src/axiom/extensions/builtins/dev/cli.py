# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi dev`` — thin argparse over the skills (ADR-056)."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext, SkillResult


def _cli_name() -> str:
    try:
        from axiom.infra.branding import get_branding

        return (get_branding().cli_name or "axi").strip()
    except Exception:
        return "axi"


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
        help="vault credential to sign in with, or 'none' (default: the one the vault holds)",
    )
    up.add_argument(
        "--with",
        dest="with",
        action="append",
        default=[],
        metavar="SRC",
        help="another source root to run from, ahead of the installed package (repeatable)",
    )
    up.add_argument("--restart", action="store_true", help="stop a running node first")
    up.add_argument("--timeout", type=float, help="seconds to wait for the app (default 90)")
    down = verbs.add_parser("down", help="stop this checkout's node")
    status = verbs.add_parser("status", help="whether this checkout's node is up")
    for verb in (up, down, status):
        verb.add_argument("--caller-goal", dest="caller_goal", default="", help=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    return p


def _render(verb: str, result: SkillResult) -> str:
    v = result.value or {}
    if verb != "up":
        return "\n".join(result.actions_taken) or "done"
    cli = _cli_name()
    lines = [
        "",
        f"  {v['url']}",
        "",
    ]
    if v.get("sign_in"):
        lines.append(f"  Sign in: '{v['sign_in']}', or with a password as {v['email']}")
    else:
        lines.append(f"  Sign in as {v['email']}")
    lines.append(f"  Password: {cli} secrets get {v['password_secret']} --reveal | pbcopy")
    if v.get("already_running"):
        lines.insert(0, "  already running")
    else:
        lines += ["", f"  log: {v['log']}", f"  stop: {cli} dev down"]
    lines += [f"  {n}" for n in v.get("notes") or []]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "verb", None) is None:
        parser.print_help()
        return 2
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillRegistry

    from .skills import register

    registry = SkillRegistry()
    register(registry)
    ctx = SkillContext(
        registry=registry,
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.dev"),
    )
    params = {
        k: val
        for k, val in vars(args).items()
        if k not in ("verb", "json") and val not in (None, [], "", False)
    }
    try:
        result = invoke_capability(registry, f"dev.{args.verb}", params, ctx, surface=CLI_SURFACE)
    except KeyError:
        print(f"dev.{args.verb} is not a registered capability", file=sys.stderr)
        return 1
    if result.ok:
        print(json.dumps(result.value, indent=2) if args.json else _render(args.verb, result))
    else:
        print("\n".join(result.errors), file=sys.stderr)
    return result.exit_code
