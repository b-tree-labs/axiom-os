# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi flags``: feature flags at the node's own command line (ADR-210).

    axi flags                                  # list (the default)
    axi flags resolve [--user U] [--account A] [--node N] [KEY]
    axi flags set KEY on|off|soon [--scope node|account|user] [--id ID]
    axi flags clear KEY [--scope ...] [--id ID]

Not ``axi features``: that noun is node functions (ADR-164), a different thing.
Every verb goes through ``invoke_capability``, so ``set`` is audited exactly as
it is from MCP or the web.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any


def _ctx():
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillContext, SkillRegistry

    from .skills import bind

    registry = SkillRegistry()
    bind(registry)
    return SkillContext(
        registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.flags")
    )


def _invoke(capability: str, params: dict[str, Any]):
    from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability

    ctx = _ctx()
    return invoke_capability(ctx.registry, capability, params, ctx, surface=CLI_SURFACE)


def get_parser() -> argparse.ArgumentParser:
    from axiom.infra.branding import get_branding

    p = argparse.ArgumentParser(
        prog=f"{get_branding().cli_name} flags", description="Feature flags: list, resolve, set"
    )
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    sub = p.add_subparsers(dest="cmd")

    ls = sub.add_parser("list", help="declared flags, defaults and stored overrides")
    ls.add_argument("--prefix", default="")

    rs = sub.add_parser("resolve", help="what a node, account and user resolve")
    rs.add_argument("key", nargs="?", default="")
    rs.add_argument("--node", default="")
    rs.add_argument("--account", default="")
    rs.add_argument("--user", default="")

    for name, helptext in (("set", "override a flag"), ("clear", "remove an override")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("key")
        if name == "set":
            sp.add_argument("value", help="on | off | soon")
        sp.add_argument("--scope", default="node", choices=("node", "account", "user"))
        sp.add_argument("--id", dest="scope_id", default="", help="the node, account or user id")
    return p


def _print_list(value: dict) -> None:
    from axiom.infra.cli_format import Column, table

    print(f"environment: {value['environment']}")
    if not value["flags"]:
        print("(no flags are declared)")
        return
    rows = []
    for row in value["flags"]:
        rows.append([row["key"], row["default"], row["owner"],
                     "protected" if row["protected"] else "", row["description"]])
        for o in row["overrides"]:
            rows.append([f"  {o['scope']}:{o['scope_id']}", o["value"], o["updated_by"],
                         "", o["updated_at"] or ""])
    cols = [Column("flag"), Column("value"), Column("owner / by"), Column(""), Column("", wrap=True)]
    print("\n".join(table(rows, cols, headers=True)))


def _print_resolve(value: dict) -> None:
    from axiom.infra.cli_format import Column, table

    who = ", ".join(f"{k}={v}" for k, v in value["for"].items() if v)
    print(f"environment: {value['environment']}  ({who})")
    rows = [[key, val, value["sources"][key]] for key, val in value["flags"].items()]
    print("\n".join(table(rows, [Column("flag"), Column("value"), Column("from")], headers=True)))


def main(argv: list[str] | None = None) -> int:
    args = get_parser().parse_args(sys.argv[1:] if argv is None else argv)
    cmd = args.cmd or "list"
    if cmd == "list":
        result = _invoke("flags.list", {"prefix": getattr(args, "prefix", "")})
    elif cmd == "resolve":
        result = _invoke(
            "flags.resolve",
            {"key": args.key, "node": args.node, "account": args.account, "user": args.user},
        )
    else:
        result = _invoke(
            "flags.set",
            {
                "key": args.key,
                "value": args.value if cmd == "set" else "clear",
                "scope": args.scope,
                "scope_id": args.scope_id,
            },
        )
    if not result.ok:
        print("\n".join(result.errors), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result.value, indent=2, default=str))
    elif cmd == "list":
        _print_list(result.value)
    elif cmd == "resolve":
        _print_resolve(result.value)
    else:
        print("\n".join(result.actions_taken))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
