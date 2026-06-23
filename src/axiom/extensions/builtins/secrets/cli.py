# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi secrets`` — operator pre-flight CLI for the secrets extension.

Per ADR-056: a thin argparse wrapper that translates flags → params dict
and dispatches to ``SkillRegistry.invoke``. All business logic lives in
the skill functions under ``secrets/skills/``.

Verbs:

- ``diagnose [<ref>]`` — probe one ref end-to-end or walk every
  registered provider kind. Pre-flight only; never prints secret values.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from axiom.infra.paths import get_user_state_dir
from axiom.infra.skills import SkillContext, SkillResult

# Ensure built-in providers are registered before we ask the registry
# what kinds it knows about.
from . import providers as _providers  # noqa: F401  (import for side effect)
from . import skills as secrets_skills


_PROG = "axi secrets"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=_PROG,
        description="secrets: operator pre-flight for the SecretStore wiring.",
    )
    p.add_argument("--json", action="store_true",
                   help="emit the SkillResult as JSON")
    sub = p.add_subparsers(dest="verb", required=True)

    diag = sub.add_parser(
        "diagnose",
        help="Probe a single SecretRef end-to-end, or walk every registered kind.",
    )
    diag.add_argument(
        "ref", nargs="?", default=None,
        help="SecretRef to probe (e.g. openbao://kv/data/x or env://NAME). "
             "Omit to walk all registered kinds.",
    )

    return p


def _terminal_prompt(prompt: str) -> str:
    return input(prompt)


def _build_ctx() -> SkillContext:
    return SkillContext(
        registry=secrets_skills.bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.secrets"),
        user_prompt=_terminal_prompt if sys.stdin.isatty() else None,
    )


def _args_to_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for k, v in vars(args).items():
        if k in ("verb", "json"):
            continue
        if v is None:
            continue
        params[k.replace("-", "_")] = v
    return params


def _emit(result: SkillResult, as_json: bool) -> int:
    if as_json:
        print(json.dumps({
            "ok": result.ok,
            "value": result.value,
            "errors": result.errors,
            "actions_taken": result.actions_taken,
        }, indent=2, default=str))
        return result.exit_code
    for action in result.actions_taken:
        print(f"• {action}")
    if result.value is not None:
        if isinstance(result.value, dict) and "items" in result.value:
            for item in result.value["items"]:
                print("  " + "  ".join(f"{k}={v}" for k, v in item.items()))
        elif isinstance(result.value, (str, int, float, bool)):
            print(result.value)
        else:
            print(json.dumps(result.value, indent=2, default=str))
    if not result.ok:
        for err in result.errors:
            print(f"ERROR: {err}", file=sys.stderr)
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    ctx = _build_ctx()
    params = _args_to_params(args)
    result = ctx.registry.invoke(f"secrets.{args.verb}", params, ctx)
    return _emit(result, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
