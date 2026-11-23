# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi app`` — thin argparse over the ``app.*`` skills (ADR-056)."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext


def _cli_name() -> str:
    try:
        from axiom.infra.branding import get_branding

        return (get_branding().cli_name or "axi").strip()
    except Exception:  # noqa: BLE001
        return "axi"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"{_cli_name()} app",
                                description="the web app build-out")
    sub = p.add_subparsers(dest="verb", required=True)
    a = sub.add_parser(
        "acceptance",
        help="every spec row, the tests carrying its ID, and pass / fail / unproven",
    )
    a.add_argument("--phase", choices=("P0", "P1", "all"), default="all",
                   help="which phase's rows to list (default: all)")
    a.add_argument("--run", action="store_true",
                   help="run the tagged tests first and cache their results")
    a.add_argument("--config", help="acceptance.toml (default: $AXIOM_ACCEPTANCE_CONFIG, else "
                   "the nearest acceptance.toml upwards, else this repository only)")
    a.add_argument("--json", action="store_true", help="machine-readable output")
    a.add_argument("--strict", action="store_true",
                   help="exit 1 unless every listed row passes (a phase gate)")
    v = sub.add_parser("variants", help="the web app's variants: port, path, realm, tenants, flags")
    v.add_argument("--config", help="variants.toml (default: <config dir>/variants.toml)")
    v.add_argument("--json", action="store_true", help="machine-readable output")
    r = sub.add_parser("run", help="start a web app variant (or --all) under the supervisor")
    which = r.add_mutually_exclusive_group(required=True)
    which.add_argument("--variant", choices=("axiom", "product", "site"),
                       help="the variant to start")
    which.add_argument("--all", action="store_true", help="start all three variants")
    r.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    r.add_argument("--log-level", default="warning")
    r.add_argument("--dry-run", action="store_true", help="print the plan; start nothing")
    r.add_argument("--config", help="variants.toml (default: <config dir>/variants.toml)")
    r.add_argument("--json", action="store_true", help="machine-readable output")
    return p


def _invoke(name: str, params: dict, as_json: bool) -> int:
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillRegistry

    from .skills import bind

    registry = SkillRegistry()
    bind(registry)
    ctx = SkillContext(registry=registry, state_dir=get_user_state_dir(),
                       logger=logging.getLogger("axi.app"))
    result = invoke_capability(registry, name, params, ctx, surface=CLI_SURFACE)
    if not result.ok:
        print("\n".join(result.errors), file=sys.stderr)
        return result.exit_code
    if as_json:
        print(json.dumps(result.value, indent=2, default=str))
    else:
        print("\n".join(result.actions_taken))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.verb == "variants":
        return _invoke("app.variants", {"config": args.config}, args.json)
    if args.verb == "run":
        return _invoke("app.run", {"variant": args.variant, "all": args.all, "host": args.host,
                                   "log_level": args.log_level, "dry_run": args.dry_run,
                                   "config": args.config}, args.json)
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillRegistry

    from .skills import bind

    registry = SkillRegistry()
    bind(registry)
    ctx = SkillContext(registry=registry, state_dir=get_user_state_dir(),
                       logger=logging.getLogger("axi.app"))
    params = {"phase": args.phase, "config": args.config}
    if args.run:
        ran = invoke_capability(registry, "app.acceptance_run", params, ctx, surface=CLI_SURFACE)
        if not ran.ok:
            print("\n".join(ran.errors), file=sys.stderr)
            return ran.exit_code
        if not args.json:
            print("\n".join(ran.actions_taken))
    result = invoke_capability(registry, "app.acceptance", params, ctx, surface=CLI_SURFACE)
    if not result.ok:
        print("\n".join(result.errors), file=sys.stderr)
        return result.exit_code
    if args.json:
        print(json.dumps(result.value, indent=2, default=str))
    else:
        print("\n".join(result.actions_taken))
    if args.strict and not result.value.get("proven"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
