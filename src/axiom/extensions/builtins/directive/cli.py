# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`axi directive` — manage version directives.

Version directives encode "this node should be on at least version X of
package Y by deadline Z." They're the receipt target for federation
broadcasts (`@all-curios:<context>` upgrade requests) but also useful
locally — Tidy's health check surfaces non-compliance as a finding.

Usage:
    axi directive add --package axi-platform --min-version 0.10.0 \\
                      --deadline 2026-05-01 --reason "security patch"
    axi directive list
    axi directive revoke <id>
"""

from __future__ import annotations

import argparse
import json
import sys

from axiom.policy.version_directive_store import (
    VersionDirective,
    add,
    list_all,
    load_active,
    revoke,
)


def _cmd_add(args) -> int:
    d = VersionDirective(
        package=args.package,
        min_version=args.min_version,
        issuer=args.issuer or "local",
        deadline=args.deadline or "",
        scope=args.scope or "",
        reason=args.reason or "",
    )
    directive_id = add(d)
    if args.json:
        print(json.dumps({"id": directive_id, "status": "added"}))
    else:
        print(f"  ✓ Added directive {directive_id}")
        print(f"    {d.package} >= {d.min_version}")
        if d.deadline:
            print(f"    Deadline: {d.deadline}")
        if d.reason:
            print(f"    Reason: {d.reason}")
    return 0


def _cmd_list(args) -> int:
    records = load_active() if args.active else list_all()
    if args.json:
        print(json.dumps([_to_dict(d) for d in records], indent=2))
        return 0
    if not records:
        print("  No directives." if not args.active else "  No active directives.")
        return 0
    print(f"  {'ID':<14} {'Package':<18} {'Min version':<14} {'Deadline':<12} {'Status'}")
    print("  " + "─" * 72)
    for d in records:
        status = "active" if d.active else "revoked"
        deadline = d.deadline or "—"
        print(f"  {d.id:<14} {d.package:<18} {d.min_version:<14} {deadline:<12} {status}")
    return 0


def _cmd_revoke(args) -> int:
    ok = revoke(args.id, reason=args.reason or "")
    if args.json:
        print(json.dumps({"id": args.id, "revoked": ok}))
        return 0 if ok else 1
    if ok:
        print(f"  ✓ Revoked directive {args.id}")
        return 0
    print(f"  Directive {args.id} not found or already revoked")
    return 1


def _to_dict(d: VersionDirective) -> dict:
    from dataclasses import asdict

    return asdict(d)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi directive",
        description="Manage version directives",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    sub = parser.add_subparsers(dest="action")

    p_add = sub.add_parser("add", help="Create a new directive")
    p_add.add_argument("--package", required=True)
    p_add.add_argument("--min-version", required=True)
    p_add.add_argument("--issuer", default="")
    p_add.add_argument("--deadline", default="", help="ISO date, e.g. 2026-05-01")
    p_add.add_argument("--scope", default="")
    p_add.add_argument("--reason", default="")

    p_list = sub.add_parser("list", help="List directives")
    p_list.add_argument("--active", action="store_true", help="Only active directives")

    p_rev = sub.add_parser("revoke", help="Revoke an active directive")
    p_rev.add_argument("id")
    p_rev.add_argument("--reason", default="")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.action:
        parser.print_help()
        return 0
    handlers = {
        "add": _cmd_add,
        "list": _cmd_list,
        "revoke": _cmd_revoke,
    }
    return handlers[args.action](args)


if __name__ == "__main__":
    sys.exit(main())
