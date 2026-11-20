# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI for ``process``: thin wrappers over the registered skills (ADR-056).

process ls                  what the harness started, with live state
process show <id>           one record, live state, findings
process stop <id> [--kill]  dry-run unless --kill (TERM group, grace, KILL)
process sweep [--auto|--kill]  report by default
process panic [--kill]      agents off + agent processes stopped; dry-run default
process resume <agent> [--yes]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from axiom.infra.paths import get_user_state_dir
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext, SkillRegistry

from .render import render
from .skills import bind


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="process", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="verb")
    sub.add_parser("ls").add_argument("--match", help="only processes whose id, kind, agent, extension or command contains this")
    sub.add_parser("show").add_argument("id")
    st = sub.add_parser("stop")
    st.add_argument("id")
    st.add_argument("--kill", action="store_true", help="actually signal (default: dry-run)")
    st.add_argument("--grace", type=float, default=None)
    sw = sub.add_parser("sweep")
    sw.add_argument("--auto", action="store_true", help="act on the policy class only")
    sw.add_argument("--kill", action="store_true", help="act on every killable finding")
    pn = sub.add_parser("panic")
    pn.add_argument("--kill", action="store_true", help="actually do it (default: dry-run)")
    rs = sub.add_parser("resume")
    rs.add_argument("agent")
    rs.add_argument("--yes", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    verb = args.verb or "ls"
    registry = SkillRegistry()
    bind(registry)
    ctx = SkillContext(
        registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.process")
    )
    params = {k: v for k, v in vars(args).items() if k not in {"verb", "json"} and v is not None}
    # The dispatch chokepoint: authority gate, action audit and telemetry live here.
    result = invoke_capability(registry, f"process.{verb}", params, ctx, surface=CLI_SURFACE)
    if args.json:
        print(
            json.dumps(
                {"ok": result.ok, "value": result.value, "errors": result.errors},
                indent=2,
                default=str,
            )
        )
    elif result.value:
        print(render(verb, result.value))
    for err in result.errors:
        print(f"ERROR: {err}", file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
