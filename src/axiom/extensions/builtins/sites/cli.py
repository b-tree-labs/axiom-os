# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The ``sites`` command: today only ``checklist <site>`` (ADR-178 D2 names the rest)."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from axiom.infra.paths import get_user_state_dir
from axiom.infra.principal import resolve_principal
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext

from . import skills as sites_skills


def _brand_cli() -> str:
    """The command the operator actually typed, whatever this distribution calls it."""
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def build_parser() -> argparse.ArgumentParser:
    cli = _brand_cli()
    p = argparse.ArgumentParser(
        prog=f"{cli} sites", description="A tenant site, administered as one record (ADR-178)"
    )
    sub = p.add_subparsers(dest="verb", required=True)
    c = sub.add_parser(
        "checklist",
        help="every readiness check for a site: ok, waiting, failing (with the fix) or not applicable",
    )
    c.add_argument("site")
    c.add_argument(
        "--min-version",
        dest="min_version",
        default="",
        help="the oldest version the intake accepts",
    )
    c.add_argument(
        "--stopped-after-h",
        dest="stopped_after_h",
        type=float,
        default=None,
        help="hours without a reading before data counts as stopped (default 24)",
    )
    c.add_argument("--json", action="store_true")
    return p


def _build_ctx() -> SkillContext:
    return SkillContext(
        registry=sites_skills.bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.sites"),
        user_prompt=None,
        principal=resolve_principal(),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    params = {"site": args.site, "min_version": args.min_version}
    if args.stopped_after_h:
        params["stopped_after_h"] = args.stopped_after_h
    ctx = _build_ctx()
    result = invoke_capability(ctx.registry, "sites.checklist", params, ctx, surface=CLI_SURFACE)
    if not result.ok:
        for err in result.errors:
            print(err, file=sys.stderr)
        return 2
    doc = dict(result.value or {})
    if args.json:
        print(json.dumps({k: v for k, v in doc.items() if k != "text"}, indent=2))
    else:
        text = str(doc.get("text") or "")
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))
    return 0 if doc.get("ready") else 1


__all__ = ["build_parser", "main"]
