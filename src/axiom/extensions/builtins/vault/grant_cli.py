# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi grant list|show|revoke``: every grant in force, in one shape (ADR-250 D4, RW-G1).

Thin wrappers over the ``grant.*`` skills (ADR-056); the projection is
:mod:`axiom.governance.grants`.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from axiom.infra.paths import get_user_state_dir
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext


def _cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=f"{_cli()} grant",
        description="Grants in force (read, act, sign, run): one list, one revoke",
    )
    sub = p.add_subparsers(dest="verb", required=True)
    ls = sub.add_parser("list", help="every grant you hold, issued or govern, earliest expiry first")
    ls.add_argument("--principal", help="only grants held by this @name:context")
    ls.add_argument("--kind", choices=("read", "act", "sign", "run"))
    ls.add_argument("--keys-file", help="the gate's keys file (default: $AXIOM_GATE_API_KEYS_FILE)")
    ls.add_argument("--json", action="store_true")
    sh = sub.add_parser("show", help="one grant by id")
    sh.add_argument("id")
    sh.add_argument("--keys-file")
    sh.add_argument("--json", action="store_true")
    rv = sub.add_parser("revoke", help="end one grant in the store that holds it")
    rv.add_argument("id")
    rv.add_argument("--reason", default="")
    rv.add_argument("--keys-file")
    rv.add_argument("--json", action="store_true")
    return p


def _registry():
    from .skills import bind_default

    return bind_default()


def main(argv: list[str] | None = None) -> int:
    from axiom.infra.cli_format import Column, kv_line, table

    args = build_parser().parse_args(argv)
    ctx = SkillContext(registry=_registry(), state_dir=get_user_state_dir(),
                       logger=logging.getLogger("axi.grant"))  # fmt: skip
    params: dict = {"keys_file": args.keys_file} if args.keys_file else {}
    if args.verb == "list":
        params.update({"principal": args.principal, "kind": args.kind})
    else:
        params["id"] = args.id
    if args.verb == "revoke":
        params["reason"] = args.reason
    result = invoke_capability(ctx.registry, f"grant.{args.verb}", params, ctx, surface=CLI_SURFACE)
    if not result.ok:
        for err in result.errors:
            print(err, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result.value, indent=2, default=str))
        return 0
    if args.verb == "list":
        rows = result.value["grants"]
        lines = table(
            [[g["kind"], g["id"], g["principal"], _scope(g["scope"]), g["expires_at"] or "none",
              g["issuer"] or "", g["parent"] or ""] for g in rows],  # fmt: skip
            [Column(header="KIND"), Column(header="ID"), Column(header="PRINCIPAL"),
             Column(header="SCOPE", wrap=True), Column(header="EXPIRES"),
             Column(header="ISSUER"), Column(header="PARENT")],  # fmt: skip
            headers=True,
        )
        print("\n".join(lines) if rows else "  (no grant in force that you may see)")
        return 0
    if args.verb == "show":
        for key, value in result.value["grant"].items():
            shown = _scope(value) if key == "scope" else ("" if value is None else str(value))
            print(kv_line(key, shown))
        return 0
    for line in result.actions_taken:
        print(kv_line("revoked", line))
    return 0


def _scope(scope: dict) -> str:
    parts = []
    for k, v in scope.items():
        if v in (None, "", [], {}):
            continue
        if isinstance(v, (list, tuple)):
            v = ",".join(str(x) for x in v)
        elif isinstance(v, dict):
            v = " ".join(f"{a}={b}" for a, b in v.items())
        parts.append(f"{k}={v}")
    return " ".join(parts)


__all__ = ["build_parser", "main"]
