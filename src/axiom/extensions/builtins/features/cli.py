# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi features`` — thin argparse over the ``features.*`` skills (ADR-056).

    features                     what this node is for, and what is on
    features enable <name>       turn on a function, a command or background-agents
    features disable <name>      turn it off again
    features role <name>         give this node a named role; `role clear` undoes it
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from axiom.infra.cli_format import Column, table
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext


def _cli_name() -> str:
    try:
        from axiom.infra.branding import get_branding

        return (get_branding().cli_name or "axi").strip()
    except Exception:  # noqa: BLE001
        return "axi"


def build_parser() -> argparse.ArgumentParser:
    cli = _cli_name()
    p = argparse.ArgumentParser(
        prog=f"{cli} features",
        description="what this node is for, and what else can be turned on without reinstalling",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="verb")
    sub.add_parser("list", help="what is on and what can be turned on (the default)")
    en = sub.add_parser("enable", help="turn on a function, a command, or background-agents")
    en.add_argument("name")
    dis = sub.add_parser("disable", help="turn off something you turned on")
    dis.add_argument("name")
    ro = sub.add_parser("role", help="give this node a named role, or 'clear' to show everything")
    ro.add_argument("name")
    ro.add_argument(
        "--set", action="append", default=[], metavar="KEY=VALUE",
        help="a setting the role reads, e.g. --set config=/srv/site/site.toml",
    )
    return p


_SOURCE = {"role": "on (role)", "feature": "on", "always": "on", "": "off"}


def render(value: dict[str, Any]) -> str:
    cli = _cli_name()
    lines: list[str] = []
    if value.get("problem"):
        lines.append(f"note: {value['problem']}")
    if value.get("confined"):
        lines.append(
            f"This node's role: {value.get('role') or 'node'} ({', '.join(value['functions'])})."
        )
        lines.append("Only its commands show. Anything below marked off can be turned on.")
    else:
        lines.append("This node has no role, so every command shows.")
        if value.get("roles"):
            lines.append(
                f"Give it one with `{cli} features role <name>`: "
                + ", ".join(sorted(value["roles"]))
            )
    lines.append("")
    rows = value.get("rows") or []
    for kind, heading in (("function", "Functions"), ("command", "Other commands"), ("service", "Services")):
        group = [r for r in rows if r["kind"] == kind]
        if not group:
            continue
        lines.append(f"{heading}:")
        cells = []
        for r in group:
            state = _SOURCE.get(r["source"], "off")
            extra = ""
            if kind == "function" and r["commands"]:
                extra = ", ".join(r["commands"])
            elif kind == "service":
                extra = "scheduled upkeep services; off on a role node until turned on"
            cells.append((r["name"], state, extra))
        lines.extend(table(cells, [Column(), Column(), Column(wrap=True)]))
        lines.append("")
    if value.get("confined"):
        lines.append(f"Turn one on: {cli} features enable <name>   ·   undo: {cli} features disable <name>")
        lines.append(f"Show everything again: {cli} features role clear")
    return "\n".join(lines).rstrip()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillRegistry

    from .skills import bind

    registry = SkillRegistry()
    bind(registry)
    ctx = SkillContext(
        registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.features")
    )
    verb = args.verb or "list"
    params: dict[str, Any] = {}
    if verb in ("enable", "disable", "role"):
        params["name"] = args.name
    if verb == "role":
        settings = {}
        for item in args.set:
            key, sep, val = item.partition("=")
            if not sep or not key:
                print(f"--set wants KEY=VALUE, got {item!r}", file=sys.stderr)
                return 2
            settings[key.strip()] = val.strip()
        params["settings"] = settings
    name = {"list": "features.list", "enable": "features.enable",
            "disable": "features.disable", "role": "features.role"}[verb]
    result = invoke_capability(registry, name, params, ctx, surface=CLI_SURFACE)
    if not result.ok:
        print("\n".join(result.errors), file=sys.stderr)
        return result.exit_code or 1
    for action in result.actions_taken:
        print(f"• {action}")
    print(json.dumps(result.value, indent=2, default=str) if args.json else render(result.value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
