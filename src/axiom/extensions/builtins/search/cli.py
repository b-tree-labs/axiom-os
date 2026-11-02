# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi search "<query>"`` — thin argparse over the ``search.query`` skill (ADR-056)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from axiom.infra.branding import get_branding
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext

from .engine import KINDS

LABELS = {"conversation": "Chat", "logbook": "Log", "corpus": "Doc"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=f"{get_branding().cli_name} search",
        description="Search conversations, logbook records and documents on this node.",
    )
    p.add_argument("query", nargs="+", help="what to find")
    p.add_argument(
        "--kind", action="append", choices=list(KINDS), help="limit to a kind (repeatable)"
    )
    # -n kept from the corpus-only `axi search` this replaces, so muscle memory works.
    p.add_argument("-n", "--limit", type=int, default=20, help="max results")
    p.add_argument("--archived", action="store_true", help="include archived conversations")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    return p


def render(value: dict[str, Any]) -> str:
    rows = value.get("results") or []
    lines: list[str] = []
    if not rows:
        lines.append(f'Nothing on this node matches "{value.get("query", "")}".')
    for r in rows:
        tag = LABELS.get(r["kind"], r["kind"])
        lines.append(f"  {tag:<5} {r['title']}")
        if r.get("snippet"):
            lines.append(f"        {r['snippet']}")
    for kind, why in (value.get("skipped") or {}).items():
        lines.append(f"  (not searched: {kind}, {why})")
    # Every result leads to its next step.
    if rows and any(r["kind"] == "conversation" for r in rows):
        cli = get_branding().cli_name
        lines.append(
            f"\nNext: open a conversation with `{cli} chat --resume <id>` (ids via --json)."
        )
    elif not rows:
        lines.append("\nNext: try fewer words, or `--kind corpus` to search documents only.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        # sys.argv[1:] still carries the verb the user typed ("search"); the
        # corpus alias this replaces learned not to search for "search <q>".
        argv = sys.argv[1:]
        if argv and argv[0] == "search":
            argv = argv[1:]
    args = build_parser().parse_args(argv)
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillRegistry

    from .skills import bind

    registry = SkillRegistry()
    bind(registry)
    ctx = SkillContext(
        registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.search")
    )
    params: dict[str, Any] = {
        "query": " ".join(args.query),
        "limit": args.limit,
        "include_archived": args.archived,
    }
    if args.kind:
        params["kinds"] = args.kind
    result = invoke_capability(registry, "search.query", params, ctx, surface=CLI_SURFACE)
    if not result.ok:
        print("\n".join(result.errors), file=sys.stderr)
        return result.exit_code
    print(json.dumps(result.value, indent=2, default=str) if args.json else render(result.value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
