# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi actors list|show``: Team at the terminal (thin wrappers, ADR-056)."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from axiom.infra.paths import get_user_state_dir
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext

from . import skills as actor_skills


def _cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"{_cli()} actors",
                                description="Team: the people, agents and nodes that can act here")
    sub = p.add_subparsers(dest="verb", required=True)
    ls = sub.add_parser("list", help="everyone and everything that can act here, in one list")
    ls.add_argument("--kind", help="person, agent or node (comma list)")
    ls.add_argument("--role", help="only actors holding this role")
    ls.add_argument("--json", action="store_true")
    sh = sub.add_parser("show", help="one actor by handle (@name:context)")
    sh.add_argument("handle")
    sh.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    from axiom.infra.cli_format import Column, kv_line, table

    args = build_parser().parse_args(argv)
    ctx = SkillContext(registry=actor_skills.bind_default(), state_dir=get_user_state_dir(),
                       logger=logging.getLogger("axi.actors"))
    if args.verb == "list":
        params = {"kind": args.kind, "role": args.role}
        result = invoke_capability(ctx.registry, "actors.list", params, ctx, surface=CLI_SURFACE)
    else:
        result = invoke_capability(ctx.registry, "actors.show", {"handle": args.handle}, ctx,
                                   surface=CLI_SURFACE)
    if not result.ok:
        for err in result.errors:
            print(err, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result.value, indent=2, default=str))
        return 0
    if args.verb == "list":
        rows = result.value["actors"]
        lines = table(
            [[r["kind_word"], r["handle"], r.get("name") or "", ", ".join(r.get("roles") or []) or "—",
              r.get("status") or "", r.get("accountable") or ("FINDING" if r.get("finding") else "")]
             for r in rows],
            [Column(header="KIND"), Column(header="HANDLE"), Column(header="NAME", wrap=True),
             Column(header="ROLES", wrap=True), Column(header="STATUS"),
             Column(header="ACCOUNTABLE")],
            headers=True,
        )
        print("\n".join(lines) if rows else "  (nobody here you may see)")
        for f in result.value["findings"]:
            print(kv_line("finding", f"{f['handle']}: {f['finding']}"))
        for source, why in result.value["skipped"].items():
            print(kv_line(f"not read: {source}", why))
        return 0
    for key, value in result.value.items():
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value) or "—"
        elif isinstance(value, dict):
            value = json.dumps(value, default=str)
        print(kv_line(key, str(value)))
    return 0


__all__ = ["build_parser", "main"]
