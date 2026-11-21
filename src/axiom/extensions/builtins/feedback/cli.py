# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi feedback``: thin argparse over the feedback skills (ADR-056).

    axi feedback bad "the export hangs"      # one action; the server captures the context
    axi feedback good
    axi feedback reports <flag>              # a flag's reports, grouped by cause
    axi feedback decide <group> stop_flag    # the owner's answer (fix_forward needs --reason)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from axiom.infra.branding import get_branding
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"{get_branding().cli_name} feedback",
                                description="Say something is good or bad; see and answer what was reported.")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="verb", required=True)
    for verdict in ("good", "bad"):
        r = sub.add_parser(verdict, help=f"report that something is {verdict}")
        r.add_argument("text", nargs="*", help="optional: one sentence")
        r.add_argument("--severe", action="store_true", help="mark a bad report severe")
        r.add_argument("--capability", help="the capability call this is about")
        r.add_argument("--outcome", choices=("failed", "refused", "unusable"))
        r.add_argument("--flag", help="the flag it is about, when you know")
    r = sub.add_parser("reports", help="a flag's reports, grouped by cause")
    r.add_argument("flag")
    r.add_argument("-n", "--limit", type=int, default=20)
    d = sub.add_parser("decide", help="answer a feedback ask you own")
    d.add_argument("group")
    d.add_argument("choice", choices=("stop_flag", "roll_back_slot", "fix_forward"))
    d.add_argument("--reason", default="")
    return p


def _params(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    if args.verb in ("good", "bad"):
        params: dict[str, Any] = {"verdict": args.verb}
        if args.text:
            params["text"] = " ".join(args.text)
        for name in ("capability", "outcome", "flag"):
            if getattr(args, name):
                params[name] = getattr(args, name)
        if args.severe:
            params["severe"] = True
        return "feedback.report", params
    if args.verb == "reports":
        return "feedback.reports", {"flag": args.flag, "limit": args.limit}
    return "feedback.decide", {"group": args.group, "choice": args.choice, "reason": args.reason}


def render(capability: str, value: dict[str, Any], actions: list[str]) -> str:
    if capability != "feedback.reports":
        return "\n".join(actions)
    from axiom.infra.cli_format import Column, table

    rows = [(g["id"], g["status"] or "-", f"{g['bad']} bad from {g['reporters']}", g["release"],
             g.get("capability") or g.get("screen") or "-") for g in value["groups"]]
    head = f"{value['flag']}: {value['bad']} bad, {value['good']} good"
    if not rows:
        return head
    cols = [Column(header="GROUP"), Column(header="STATUS"), Column(header="REPORTS"),
            Column(header="RELEASE"), Column(header="AT", wrap=True)]
    return "\n".join([head, *table(rows, cols, headers=True)])


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
        if argv and argv[0] == "feedback":
            argv = argv[1:]
    args = build_parser().parse_args(argv)
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillRegistry

    from .skills import bind

    registry = SkillRegistry()
    bind(registry)
    ctx = SkillContext(registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.feedback"))
    capability, params = _params(args)
    result = invoke_capability(registry, capability, params, ctx, surface=CLI_SURFACE)
    if not result.ok:
        print("\n".join(result.errors), file=sys.stderr)
        return result.exit_code
    print(json.dumps(result.value, indent=2, default=str) if args.json
          else render(capability, result.value, list(result.actions_taken)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
