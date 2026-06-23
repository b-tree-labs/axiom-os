# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi skills`` — thin CLI over the ``skills.*`` registry namespace.

PR-1 (ADR-063) ships a single verb, ``emit-md``, that wraps
:func:`axiom.infra.skills_emit.run`. The CLI does no logic of its own
per ADR-056; it parses argv, builds params + ctx, dispatches, and
returns the SkillResult exit code."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from axiom.infra.skills import SkillContext, default_registry
from axiom.infra import skills_emit


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="axi skills",
        description="Inspect + maintain the SkillRegistry surface.",
    )
    sub = p.add_subparsers(dest="verb", required=True)

    emit = sub.add_parser(
        "emit-md",
        help="Generate SKILL.md + AEOS provides blocks (ADR-063).",
    )
    emit.add_argument("--ext", default=None,
                      help="Restrict to a single extension name.")
    emit.add_argument("--check", action="store_true",
                      help="Don't write; fail with non-zero on drift.")
    emit.add_argument("--only", default=None,
                      help="Comma-separated skill-name allowlist.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # Bind the generator into the default registry so spec() is populated.
    skills_emit.bind_default()

    ctx = SkillContext(
        registry=default_registry(),
        state_dir=Path.cwd(),
        logger=logging.getLogger("axi.skills"),
        user_prompt=None,
    )

    if args.verb == "emit-md":
        params = {
            "ext": args.ext,
            "check": bool(args.check),
            "only": args.only,
        }
        result = default_registry().invoke("skills.emit_md", params, ctx)
        for line in result.actions_taken:
            print(line)
        for err in result.errors:
            print(err, file=sys.stderr)
        return result.exit_code

    return 2  # unreachable — argparse enforces required subparser


if __name__ == "__main__":
    raise SystemExit(main())
