# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi principal`` — thin argparse wrappers over the skill functions.

Per ADR-056 no handler logic lives here: every verb maps 1:1 onto a function in
``skills/`` so the CLI, the MCP tools, and chat all drive identical code. The
noun is purpose-named (``principal``), not agent-named — DESK is the persona, not
the command.
"""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path
from typing import Any

from .skills import route as route_skill
from .skills import setup as setup_skill
from .skills import status as status_skill
from .skills import verify as verify_skill

__all__ = ["build_parser", "main"]

_VERBS = {
    "setup": setup_skill.run,
    "status": status_skill.run,
    "route": route_skill.run,
    "verify": verify_skill.run,
}


def _brand_cli() -> str:
    """The command the operator actually typed.

    Hardcoding "axi" makes a consumer distribution's CLI advertise a command
    that is not on the operator's PATH. The same shape as every other CLI
    module here.
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{_brand_cli()} principal",
        description="Who this harness works for, and what actually reaches them.",
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    p_setup = sub.add_parser("setup", help="run or resume the interview, then prove delivery")
    p_setup.add_argument(
        "--declare",
        metavar="FILE",
        help="TOML declaration for a NON-human principal (service, agent, node, org): "
        "skips the interview and proves endpoints by machine round trip",
    )
    sub.add_parser("status", help="the principal record and per-endpoint health")
    sub.add_parser("verify", help="re-run the comms test on every endpoint")

    p_route = sub.add_parser("route", help="explain which channel a topic would take")
    p_route.add_argument("topic", help="topic class, e.g. incident")
    p_route.add_argument("--urgency", type=int, default=5)
    p_route.add_argument("--at", help="ISO timestamp to evaluate against (default: now)")

    for p in (parser, *sub.choices.values()):
        p.add_argument("--json", action="store_true", help="machine-readable output")
        p.add_argument(
            "--config-dir",
            metavar="DIR",
            help="write/read the record here instead of the default config dir "
            "(so a trial run cannot touch the real principal)",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    params: dict[str, Any] = {
        k: v for k, v in vars(args).items() if k not in {"verb", "json"} and v is not None
    }
    # A declaration file is the non-human path (spec §3.3); the interview stays
    # the only way a person's record gets made.
    declare = params.pop("declare", None)
    if declare:
        try:
            params["declaration"] = tomllib.loads(Path(declare).read_text())
        except (OSError, tomllib.TOMLDecodeError) as exc:
            print(f"error: cannot read declaration {declare}: {exc}")
            return 1

    result = _VERBS[args.verb](params, None)

    if getattr(args, "json", False):
        print(
            json.dumps({"ok": result.ok, "value": result.value, "errors": result.errors}, indent=2)
        )
    else:
        for line in result.actions_taken:
            print(line)
        for err in result.errors:
            print(f"error: {err}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
