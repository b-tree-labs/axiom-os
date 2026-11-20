# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The ``sites`` command: ``list``, ``show <site>`` and ``checklist <site>`` (ADR-178 D2)."""

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
    c.add_argument("site", nargs="?", default="", help="the site (not needed with --first-run)")
    c.add_argument(
        "--first-run",
        dest="first_run",
        action="store_true",
        help="the five first-run steps (connect data, invite, branding, landing, first chart)",
    )
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
    ls = sub.add_parser("list", help="the sites you may see here, and which the scope selector chose")
    ls.add_argument("--json", action="store_true")
    sh = sub.add_parser("show", help="one site you may see: its name and former ids")
    sh.add_argument("site")
    sh.add_argument("--json", action="store_true")
    return p


def _build_ctx() -> SkillContext:
    return SkillContext(
        registry=sites_skills.bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.sites"),
        user_prompt=None,
        principal=resolve_principal(),
    )


def _tenancy(args) -> int:
    ctx = _build_ctx()
    name = f"sites.{args.verb}"
    params = {"site": args.site} if args.verb == "show" else {}
    result = invoke_capability(ctx.registry, name, params, ctx, surface=CLI_SURFACE)
    if not result.ok:
        for err in result.errors:
            print(err, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result.value, indent=2))
    elif args.verb == "list":
        from axiom.infra.cli_format import Column, table

        rows = result.value["sites"]
        chosen = bool(result.value["selected"])
        lines = table(
            [[r["id"], r["name"], "yes" if (r["selected"] and chosen) else ""] for r in rows],
            [Column(header="SITE"), Column(header="NAME", wrap=True), Column(header="CHOSEN")],
            headers=True,
        )
        print("\n".join(lines) if rows else "  (no sites you may see)")
    else:
        from axiom.infra.cli_format import kv_line

        v = result.value
        print(kv_line("site", v["id"]))
        print(kv_line("name", v["name"]))
        if v.get("also_known_as"):
            print(kv_line("also known as", ", ".join(v["also_known_as"])))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.verb in ("list", "show"):
        return _tenancy(args)
    params = {"site": args.site, "min_version": args.min_version}
    if args.first_run:
        params = {"kind": "first_run"}
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
    if args.first_run:
        return 0 if doc.get("complete") else 1
    return 0 if doc.get("ready") else 1


__all__ = ["build_parser", "main"]
