# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi receipts`` — thin wrappers over the receipts.* skills (ADR-056).

Verbs: ``today`` (the oversight brief), ``receipt`` (one evidence
record), ``direct`` (read/SET the focus — the CLI is a gated surface on
the node, so unlike the MCP courier it may set, not just propose).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from axiom.infra.branding import get_branding
from axiom.infra.paths import get_user_state_dir
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext, SkillRegistry


def _bind_registry() -> SkillRegistry:
    """One binding for every surface. It lives in the skills package so
    the CLI, the HTTP route and a test all get the SAME registry — the
    three used to disagree about whether ``receipts.decide`` existed."""
    from axiom.extensions.builtins.receipts.skills import bind_default

    return bind_default()


def _ctx() -> SkillContext:
    return SkillContext(
        registry=_bind_registry(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.receipts"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{get_branding().cli_name} receipts", description=__doc__
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    p_today = sub.add_parser("today", help="the oversight brief")
    p_today.add_argument("--site", default=None)
    p_today.add_argument("--json", action="store_true")
    p_today.add_argument(
        "--no-snapshot", action="store_true", help="compose without recording the delta baseline"
    )

    p_receipt = sub.add_parser("receipt", help="one entity's evidence record")
    p_receipt.add_argument("entity_id")
    p_receipt.add_argument("claim_kind")
    p_receipt.add_argument("--entity-kind", default="node")

    p_direct = sub.add_parser("direct", help="read or set the day focus")
    p_direct.add_argument("text", nargs="?", default=None)
    p_direct.add_argument("--site", default="")
    p_direct.add_argument("--by", default="@operator:cli")

    args = parser.parse_args(argv)

    ctx = _ctx()
    if args.verb == "today":
        result = invoke_capability(
            ctx.registry,
            "receipts.today",
            {"site": args.site, "snapshot": not args.no_snapshot},
            ctx,
            surface=CLI_SURFACE,
        )
        if not result.ok:
            print("; ".join(result.errors), file=sys.stderr)
            return 1
        print(json.dumps(result.value["brief"], indent=2) if args.json else result.value["text"])
        return 0

    if args.verb == "receipt":
        from axiom.extensions.builtins.receipts.skills.receipt import run

        result = run(
            {
                "entity_kind": args.entity_kind,
                "entity_id": args.entity_id,
                "claim_kind": args.claim_kind,
            }
        )
        if not result.ok:
            print("; ".join(result.errors), file=sys.stderr)
            return 1
        print(json.dumps(result.value, indent=2))
        return 0

    from axiom.extensions.builtins.receipts.skills.direct import run

    result = run(
        {
            "site": args.site,
            "text": args.text,
            "mode": "get" if args.text is None else "set",
            "set_by": args.by,
        }
    )
    if not result.ok:
        print("; ".join(result.errors), file=sys.stderr)
        return 1
    print(json.dumps(result.value, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
