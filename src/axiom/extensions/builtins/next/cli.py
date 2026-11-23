# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi next`` — thin argparse over the ``next.suggest`` skill (ADR-056)."""

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
    p = argparse.ArgumentParser(
        prog=f"{get_branding().cli_name} next",
        description="What you would plausibly do next, ranked, with why.",
    )
    p.add_argument("--about", default=None, help="what you are looking at (a label), to rank by it")
    p.add_argument("-n", "--limit", type=int, default=5)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    return p


def render(value: dict[str, Any]) -> str:
    cards = value.get("cards") or []
    if not cards:
        return "Nothing to suggest right now."
    lines = []
    for i, c in enumerate(cards, 1):
        lines.append(f"  {i}. {c['label']}")
        lines.append(f"     {c['why']}")
    cli = get_branding().cli_name
    lines.append(f"\nNext: ask the first with `{cli} chat`, or pick one with `{cli} next --json`.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
        if argv and argv[0] == "next":
            argv = argv[1:]
    args = build_parser().parse_args(argv)
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillRegistry

    from .skills import bind

    registry = SkillRegistry()
    bind(registry)
    ctx = SkillContext(
        registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.next")
    )
    params: dict[str, Any] = {"limit": args.limit}
    if args.about:
        params["view_context"] = {"label": args.about}
    result = invoke_capability(registry, "next.suggest", params, ctx, surface=CLI_SURFACE)
    if not result.ok:
        print("\n".join(result.errors), file=sys.stderr)
        return result.exit_code
    print(json.dumps(result.value, indent=2) if args.json else render(result.value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
