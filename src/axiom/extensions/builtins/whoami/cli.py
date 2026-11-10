# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi whoami`` — thin argparse over the ``whoami.report`` skill (ADR-056)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext

#: flag -> section. ``--identity`` reads better than ``--you`` at a terminal.
FLAGS = {
    "identity": "you",
    "software": "software",
    "node": "node",
    "site": "site",
    "access": "access",
    "dev": "dev",
    "harness": "harness",
    "llm": "llm",
    "env": "env",
}

TITLES = {
    "you": "You",
    "software": "Software",
    "node": "This node",
    "site": "Site and federation",
    "access": "Credentials and sign-in",
    "dev": "Dev nodes",
    "harness": "Agent harnesses (MCP)",
    "llm": "Model providers",
    "env": "Settings in this shell",
}


def _cli_name() -> str:
    try:
        from axiom.infra.branding import get_branding

        return (get_branding().cli_name or "axi").strip()
    except Exception:
        return "axi"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=f"{_cli_name()} whoami",
        description="who you are here, what this node is, and what it is connected to",
    )
    for flag, section in FLAGS.items():
        p.add_argument(
            f"--{flag}", action="store_true", help=f"only the {TITLES[section].lower()} section"
        )
    p.add_argument(
        "--all",
        action="store_true",
        help="every section, including the slower model-provider probe",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    return p


def _rows(key: str, value: Any, depth: int = 0) -> list[tuple[str, str]]:
    """Flatten one value into (label, text) rows; nesting shows as indent in the label."""
    label = "  " * depth + key
    if value in (None, [], {}, ""):
        return [(label, "-")]
    if isinstance(value, dict):
        rows = [(label, "")]
        for k, v in value.items():
            rows += _rows(k, v, depth + 1)
        return rows
    if isinstance(value, list) and isinstance(value[0], dict):
        rows = [(label, "")]
        for item in value:
            rows.append(
                (
                    "  " * (depth + 1) + "-",
                    ", ".join(f"{k}={v}" for k, v in item.items() if v not in (None, "")),
                )
            )
        return rows
    if isinstance(value, list):
        return [(label, ", ".join(str(v) for v in value))]
    return [(label, str(value))]


def render(report: dict[str, Any]) -> str:
    from axiom.infra.cli_format import Column, table, terminal_width

    lines: list[str] = []
    for name, body in report.items():
        if name == "warnings":
            continue
        lines.append(TITLES.get(name, name))
        rows: list[tuple[str, str]] = []
        if isinstance(body, dict):
            for k, v in body.items():
                rows += _rows(k, v)
        lines += table(rows, [Column(), Column(wrap=True)], width=terminal_width())
        lines.append("")
    warns = report.get("warnings") or []
    if warns:
        lines.append("Worth knowing")
        lines += table(
            [["!", w] for w in warns], [Column(), Column(wrap=True)], width=terminal_width()
        )
    else:
        lines.append("Nothing looks out of place.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillRegistry

    from .skills import bind

    registry = SkillRegistry()
    bind(registry)
    ctx = SkillContext(
        registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.whoami")
    )
    chosen = [section for flag, section in FLAGS.items() if getattr(args, flag)]
    params: dict[str, Any] = {"all": bool(args.all)}
    if chosen:
        params["sections"] = chosen
    result = invoke_capability(registry, "whoami.report", params, ctx, surface=CLI_SURFACE)
    if not result.ok:
        print("\n".join(result.errors), file=sys.stderr)
        return result.exit_code
    print(json.dumps(result.value, indent=2, default=str) if args.json else render(result.value))
    return 0
