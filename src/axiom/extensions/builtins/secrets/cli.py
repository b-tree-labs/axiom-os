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

    rot = sub.add_parser(
        "rotate",
        help="Rotate one secret now (--force = the leaked-key closer). "
             "Never prints the secret value.",
    )
    rot.add_argument("ref", help="SecretRef to rotate (e.g. openbao://kv/data/x).")
    rot.add_argument(
        "--strategy", default="provider-native",
        help="Rotation strategy: provider-native (backend rotates itself) or "
             "hitl (human supplies the new value). Default: provider-native.",
    )
    rot.add_argument(
        "--force", action="store_true",
        help="Rotate regardless of cadence — the leaked-key closer.",
    )
    rot.add_argument(
        "--value", default=None,
        help="hitl only: the new credential value. Omit to be prompted "
             "(interactive) — never pass a real secret on a shared shell history.",
    )
    rot.add_argument(
        "--overlap", type=int, default=0,
        help="Overlap window in seconds the previous credential stays valid "
             "before retirement. 0 (default) retires it inline.",
    )
    rot.add_argument(
        "--cadence", type=int, default=None,
        help="Rotation cadence in seconds (for scheduled rotation policy). "
             "Omit for force-only.",
    )

    exp = sub.add_parser(
        "exposed",
        help="A credential appeared on an observable surface (transcript, log, "
             "chat). Records the exposure, then force-rotates. Never prints "
             "the secret value.",
    )
    exp.add_argument("ref", help="SecretRef that was exposed (e.g. openbao://kv/data/x).")
    exp.add_argument(
        "--where", required=True,
        help="Surface the credential appeared on: transcript, log, chat, url, …",
    )
    exp.add_argument(
        "--detail", default=None,
        help="Optional context for the audit trail (session id, file, message link).",
    )
    exp.add_argument(
        "--strategy", default="provider-native",
        help="Rotation strategy (same as rotate): provider-native or hitl.",
    )
    exp.add_argument(
        "--value", default=None,
        help="hitl only: the new credential value. Omit to be prompted.",
    )
    exp.add_argument(
        "--overlap", type=int, default=0,
        help="Overlap window in seconds before the leaked credential retires. "
             "Default 0: retire the leaked credential inline.",
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
