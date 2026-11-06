# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi charts`` — thin argparse over ``charts.suggest``, ``charts.render`` and ``charts.saved`` (ADR-056)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from axiom.infra.branding import get_branding
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext


def build_parser() -> argparse.ArgumentParser:
    cli = get_branding().cli_name
    p = argparse.ArgumentParser(prog=f"{cli} charts", description="Charts from data: which kinds fit, and the drawing.")
    sub = p.add_subparsers(dest="verb", required=True)
    s = sub.add_parser("suggest", help="which charts a result can be drawn as, ranked for a question")
    s.add_argument("result", help="a JSON file of the result, or - for stdin")
    s.add_argument("-q", "--question", default="", help="what was asked, to rank by")
    s.add_argument("--prefer", default="", help="kinds to put first, comma-separated (only those that fit)")
    s.add_argument("--json", action="store_true", help="the whole block, machine-readable")
    r = sub.add_parser("render", help="draw a ChartSpec 2.0 to SVG")
    r.add_argument("spec", help="a JSON file of the spec, or - for stdin")
    r.add_argument("-o", "--out", default="-", help="where the SVG goes (default stdout)")
    r.add_argument("--width", type=int, default=None)
    v = sub.add_parser("saved", help="your saved charts by catalog id; with an id, draw that one")
    v.add_argument("id", nargs="?", default="", help="a saved chart's catalog id")
    v.add_argument("--json", action="store_true", help="the whole result, machine-readable")
    v.add_argument("--width", type=int, default=None)
    return p


def _read(path: str) -> Any:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    return json.loads(text)


def render_suggestions(value: dict[str, Any]) -> str:
    if not value.get("suggestions"):
        return f"No chart: {value.get('quiet') or 'nothing here draws honestly'}."
    lines = [f"Profile: {value['profile']['form']}, {value['profile']['series']} series, "
             f"{value['profile']['rows']} rows; intents: {', '.join(value.get('intents') or []) or 'none'}"]
    for i, s in enumerate(value["suggestions"], 1):
        lines.append(f"  {i}. {s['label']}: {s['reason']}")
    more = [m["label"] for m in value.get("more") or []]
    if more:
        lines.append(f"  also fits: {', '.join(more)}")
    lines.append(f"\nNext: draw one with `{get_branding().cli_name} charts suggest <file> --json` and "
                 f"`{get_branding().cli_name} charts render`.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
        if argv and argv[0] == "charts":
            argv = argv[1:]
    args = build_parser().parse_args(argv)
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillRegistry

    from .skills import bind

    registry = SkillRegistry()
    bind(registry)
    ctx = SkillContext(registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.charts"))
    if args.verb == "suggest":
        params: dict[str, Any] = {"result": _read(args.result), "question": args.question, "prerender": False}
        if args.prefer:
            params["prefer"] = args.prefer
        result = invoke_capability(registry, "charts.suggest", params, ctx, surface=CLI_SURFACE)
    elif args.verb == "saved":
        params = {"id": args.id} if args.id else {}
        if args.width:
            params["width"] = args.width
        result = invoke_capability(registry, "charts.saved", params, ctx, surface=CLI_SURFACE)
    else:
        params = {"spec": _read(args.spec)}
        if args.width:
            params["width"] = args.width
        result = invoke_capability(registry, "charts.render", params, ctx, surface=CLI_SURFACE)
    if not result.ok:
        print("\n".join(result.errors), file=sys.stderr)
        return result.exit_code or 1
    if args.verb == "suggest":
        print(json.dumps(result.value, indent=2) if args.json else render_suggestions(result.value))
    elif args.verb == "saved":
        value = result.value
        if args.json:
            print(json.dumps(value, indent=2))
        elif "charts" in value:
            rows = value["charts"] or []
            print("\n".join(f"{c['id']}  {c['group']} / {c['title'] or c['kind']}" for c in rows)
                  or "No saved charts.")
        else:
            print(value["svg"])
    else:
        svg = result.value["svg"]
        if args.out == "-":
            print(svg)
        else:
            Path(args.out).write_text(svg, encoding="utf-8")
            print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
