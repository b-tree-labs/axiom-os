# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi hygiene`` — hygiene + resource-stewardship CLI.

Per ADR-056: CLI verbs are thin wrappers over registered skill
functions. Business logic lives in ``hygiene.skills`` (today, those
skills delegate to legacy ``_cmd_X`` handlers in ``_legacy_cli.py``;
a future refactor lifts formatting into structured ``SkillResult``).

Verb grammar (per spec-aeos-0.1 §4.3.1 + the 2026-05-30 audit):
imperative verbs only; resources are positional. The pre-migration
bare-noun verbs are consolidated:

    OLD `axi tidy <noun>`          NEW `axi hygiene <verb> <resource>`
    worktrees             →  list worktrees
    branches              →  list branches
    vitals                →  stat vitals
    health                →  stat health
    ci                    →  stat ci
    drift                 →  stat drift
    retention             →  stat retention

TIDY remains the agent-persona name (the LLM character used for
diagnose/troubleshoot reasoning); the CLI noun moves to the
purpose-named ``hygiene`` per the 2026-05-30 noun-convention policy.
"""

from __future__ import annotations

import argparse
from typing import Any

from . import _legacy_cli as _legacy
from . import skills as hygiene_skills


_PROG = "axi hygiene"


def build_parser() -> argparse.ArgumentParser:
    """Build the new grammar-compliant parser, reusing the legacy
    sub-verbs' own argparse setup wherever possible.

    The trick: legacy `build_parser()` already declares all the
    per-verb flags. We attach those subparsers to our new verb tree.
    """
    legacy_parser = _legacy.build_parser()
    # legacy parser's subparsers cache is the source of truth for
    # per-verb flag definitions; we re-attach the relevant ones.
    legacy_sub = next(
        a for a in legacy_parser._subparsers._actions
        if isinstance(a, argparse._SubParsersAction)
    )
    legacy_subs = legacy_sub.choices

    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="hygiene — autonomous resource stewardship",
    )
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of human format (where supported)")
    sub = parser.add_subparsers(dest="verb")

    # ---- imperative-leaf verbs (1:1 from legacy) ------------------------
    for leaf in ("status", "ls", "clean", "purge", "diagnose",
                 "discover", "propose", "approve", "deny"):
        if leaf in legacy_subs:
            # Re-parent the legacy subparser under our new dispatcher.
            legacy_p = legacy_subs[leaf]
            sub.add_parser(leaf, parents=[legacy_p], add_help=False,
                           conflict_handler="resolve",
                           help=legacy_p.description or "")

    # ---- consolidating verbs --------------------------------------------
    # `list <resource>` (worktrees / branches)
    list_p = sub.add_parser("list", help="List resource (worktrees|branches).")
    list_sub = list_p.add_subparsers(dest="resource", required=True)
    for res in ("worktrees", "branches"):
        if res in legacy_subs:
            legacy_p = legacy_subs[res]
            list_sub.add_parser(res, parents=[legacy_p], add_help=False,
                                conflict_handler="resolve")

    # `stat <resource>` (vitals|health|ci|drift|retention)
    stat_p = sub.add_parser("stat", help="Stat resource (vitals|health|ci|drift|retention).")
    stat_sub = stat_p.add_subparsers(dest="resource", required=True)
    for res in ("vitals", "health", "ci", "drift", "retention"):
        if res in legacy_subs:
            legacy_p = legacy_subs[res]
            stat_sub.add_parser(res, parents=[legacy_p], add_help=False,
                                conflict_handler="resolve")

    return parser


def _args_to_params(args: argparse.Namespace) -> dict[str, Any]:
    return {k: v for k, v in vars(args).items() if v is not None}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not getattr(args, "verb", None):
        args.verb = "status"
    # Provide the legacy `.action` field some code paths still consult.
    args.action = args.verb

    from axiom.infra.skills import SkillContext
    from axiom.infra.paths import get_user_state_dir
    import logging

    registry = hygiene_skills.bind_default()
    ctx = SkillContext(
        registry=registry,
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.hygiene"),
        user_prompt=None,
    )
    params = _args_to_params(args)
    result = registry.invoke(f"hygiene.{args.verb}", params, ctx)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
