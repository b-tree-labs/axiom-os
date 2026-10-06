# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi program`` — thin wrappers over the ``program.*`` skills (ADR-056).

Verbs: ``status`` (the parameterized read), ``render`` (data file → one
static status page), ``validate`` (the data file against its schema).
Each handler translates flags into a params dict and dispatches through
``invoke_capability`` — no business logic lives here.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from axiom.infra.branding import get_branding
from axiom.infra.paths import get_user_state_dir
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext, SkillResult

from .skills import bind_default
from .skills.status import FORMATS as _FORMATS
from .skills.status import SCOPES as _SCOPES


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{get_branding().cli_name} program",
        description="Program tracking: status, render, validate over the program data file.",
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    p_status = sub.add_parser("status", help="items with owner, dates, status, pct, links")
    p_status.add_argument("--scope", required=True, choices=_SCOPES)
    p_status.add_argument("--key", help="the principal, lane id, or item id the scope reads")
    p_status.add_argument("--fmt", default="brief", choices=_FORMATS)
    p_status.add_argument("--data", help="path to the program data file")
    p_status.add_argument("--json", action="store_true", help="emit the SkillResult as JSON")

    p_render = sub.add_parser("render", help="render the data file to one static status page")
    p_render.add_argument("--data", help="path to the program data file")
    p_render.add_argument("--out", help="output directory for the page")
    p_render.add_argument("--json", action="store_true", help="emit the SkillResult as JSON")

    p_validate = sub.add_parser("validate", help="check the data file against its schema")
    p_validate.add_argument("--data", help="path to the program data file")
    p_validate.add_argument("--json", action="store_true", help="emit the SkillResult as JSON")

    return parser


def _ctx() -> SkillContext:
    return SkillContext(
        registry=bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.program"),
        user_prompt=_terminal_prompt if sys.stdin.isatty() else None,
    )


def _terminal_prompt(prompt: str) -> str:
    return input(prompt)


def _emit(result: SkillResult, as_json: bool) -> int:
    if as_json:
        print(
            json.dumps({"ok": result.ok, "value": result.value, "errors": result.errors}, indent=2)
        )
        return result.exit_code
    if not result.ok:
        print("; ".join(result.errors), file=sys.stderr)
        return 1
    print(json.dumps(result.value, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    ctx = _ctx()
    params = {
        key: value
        for key, value in vars(args).items()
        if key not in ("verb", "json") and value is not None
    }
    result = invoke_capability(
        ctx.registry,
        f"program.{args.verb}",
        params,
        ctx,
        surface=CLI_SURFACE,
    )
    return _emit(result, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
