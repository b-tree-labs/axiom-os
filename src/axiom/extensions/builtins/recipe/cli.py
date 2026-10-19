# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi recipe`` - thin argparse over the ``recipe.*`` skills (ADR-056)."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from axiom.infra.paths import get_user_state_dir
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext, SkillRegistry


def _cli_name() -> str:
    try:
        from axiom.infra.branding import get_branding

        return (get_branding().cli_name or "axi").strip()
    except Exception:  # noqa: BLE001
        return "axi"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"{_cli_name()} recipe", description="vetted chains of skills")
    sub = p.add_subparsers(dest="verb", required=True)
    sub.add_parser("list", help="the published recipes")
    for verb, help_ in (("check", "validate a recipe against what is installed"), ("plan", "dry-run a recipe")):
        s = sub.add_parser(verb, help=help_)
        s.add_argument("id")
        if verb == "plan":
            s.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from axiom.extensions.builtins.recipe import skills as recipe_skills

    registry = SkillRegistry()
    recipe_skills.bind(registry)
    _bind_everything(registry)
    ctx = SkillContext(
        registry=registry,
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.recipe"),
        user_prompt=None,
    )
    params: dict = {}
    if args.verb != "list":
        params["id"] = args.id
    if args.verb == "plan":
        params["params"] = dict(p.split("=", 1) for p in args.param if "=" in p)
    result = invoke_capability(registry, f"recipe.{args.verb}", params, ctx, surface=CLI_SURFACE)
    if result.value is not None:
        print(json.dumps(result.value, indent=2, default=str))
    for err in result.errors:
        print(err, file=sys.stderr)
    return result.exit_code


def _bind_everything(registry: SkillRegistry) -> None:
    """Bind every discovered extension's skills so a recipe is checked against what is installed."""
    import importlib

    from axiom.extensions.discovery import _extension_source_package, surfaced_extensions

    for ext in surfaced_extensions():
        root = _extension_source_package(ext)
        name = getattr(ext, "name", None)
        if not root or not name:
            continue
        try:
            binder = getattr(importlib.import_module(f"{root}.extensions.builtins.{name}.skills"), "bind", None)
            if callable(binder):
                binder(registry)
        except Exception:  # noqa: BLE001 - one extension that will not import does not hide the rest
            continue


if __name__ == "__main__":
    raise SystemExit(main())
