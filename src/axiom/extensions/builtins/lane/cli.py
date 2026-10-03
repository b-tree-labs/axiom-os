# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi lane`` — thin argparse over the skills (ADR-056).

No logic lives here. Every verb maps to a skill function so the CLI, MCP and
agent-tool surfaces cannot drift from one another.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext, SkillResult

from .skills import claim, doctor, hold, list_lanes, release


def _cli_name() -> str:
    """The command the operator actually typed, not the platform's own name.

    `axi` is the platform CLI; a consumer distribution rebrands it, so a
    hardcoded "axi lane" tells somebody running `neut` to type a command
    that does not exist on their machine. Falls back to "axi" when no
    branding is registered, which is what an unbranded install is.
    """
    try:
        from axiom.infra.branding import get_branding

        return (get_branding().cli_name or "axi").strip()
    except Exception:
        return "axi"


def build_parser() -> argparse.ArgumentParser:
    """The `axi lane` parser, standalone.

    No argument, and it RETURNS the parser. That is the shape the CLI
    discovers by name: `axiom_cli` does `getattr(mod, "build_parser")()`
    and reads `.description` off what comes back. This took `sub` and
    mutated it, so discovery raised TypeError — and unlike the vocabulary
    scanner, that call site does not catch, so a clean-container `axi
    status` died on a stack trace rather than on anything to do with lanes.
    """
    p = argparse.ArgumentParser(
        prog=f"{_cli_name()} lane",
        description="isolated development lanes on a shared machine",
    )
    verbs = p.add_subparsers(dest="verb", required=True)

    c = verbs.add_parser("claim", help="take a lane for this checkout")
    c.add_argument("name", nargs="?", help="default: derived from the checkout directory")
    c.add_argument("--branch")
    c.add_argument("--owner")
    c.add_argument("--note")
    c.add_argument("--dsn-var", dest="dsn_var")
    c.add_argument("--tree", action="append", dest="trees", default=[])
    c.add_argument("--venv", action="append", dest="venvs", default=[])
    c.add_argument("--ports", nargs=2, type=int)
    c.add_argument("--replace", action="store_true")
    c.set_defaults(fn=claim.run)

    verbs.add_parser("list", help="every claimed lane").set_defaults(fn=list_lanes.run)

    r = verbs.add_parser("release", help="give a lane back")
    r.add_argument("name")
    r.add_argument(
        "--drop-database",
        dest="drop_database",
        action="store_true",
        help="print the drop command; never runs it",
    )
    r.set_defaults(fn=release.run)

    h = verbs.add_parser("hold", help="say which shared files this lane is editing")
    h.add_argument("paths", nargs="+", help="repo-relative, exactly as another session would type them")
    h.set_defaults(fn=hold.run, verb="hold")

    dr = verbs.add_parser("drop", help="release files this lane was holding")
    dr.add_argument("paths", nargs="+")
    dr.set_defaults(fn=hold.drop, verb="drop")

    d = verbs.add_parser("doctor", help="the registry against the machine")
    d.add_argument(
        "--explain", action="store_true", help="ask the local reasoning model which finding matters"
    )
    d.set_defaults(fn=doctor.run)

    for verb in (c, r, d, h, dr, p):
        verb.add_argument(
            "--caller-goal",
            dest="caller_goal",
            default="",
            help="one sentence: what you are trying to do (ADR-139)",
        )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    return p


def _render(verb: str, result: SkillResult) -> str:
    v = result.value or {}
    if verb == "doctor":
        return v.get("text") or "no findings"
    if verb == "list":
        # Through `cli_format.table` rather than hand-padded columns. A lane's
        # branch name is the one cell here with no bound on its length, and a
        # fixed layout hands the terminal a line it then hard-wraps — the tail
        # lands at an indent belonging to no column and reads as a second row.
        # The table owns its width instead and wraps that cell within itself.
        from axiom.infra.cli_format import Column, table

        lanes = v.get("lanes", [])
        reserved = v.get("reserved", [])
        if not lanes and not reserved:
            return "  no lanes claimed"
        body = [
            [
                "UP" if r["up"] else "down",
                r["lane"],
                f":{r['front']}/{r['api']}",
                r["database"] or f"({r['dsn_var']})",
                r["branch"],
            ]
            for r in lanes
        ]
        body += [["--", "(reserved)", f":{x['port']}", "", x["what"]] for x in reserved]
        return "\n".join(
            table(
                body,
                [
                    Column("", align="left"),
                    Column("lane"),
                    Column("ports"),
                    Column("database"),
                    Column("branch", wrap=True),
                ],
                headers=True,
            )
        )
    if verb == "claim":
        lines = [f"claimed lane {v['lane']} on :{v['front']}/{v['api']}", ""]
        lines += [f"    export {k}={q}" for k, q in sorted(v.get("exports", {}).items())]
        if v.get("next"):
            lines += ["", *(f"    {n}" for n in v["next"])]
        return "\n".join(lines)
    return "\n".join(result.actions_taken) or "done"


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    """The entry point the manifest names, with the signature a `cmd` has.

    This took `(args, ctx)` and the dispatcher calls `main(argv)`, so every
    `axi lane ...` ended at `main() missing 2 required positional arguments`.
    The verb never ran — the extension shipped, appeared in `axi --help`, and
    could not be invoked.

    Nothing was wrong below this line, which is why it survived review: the
    dispatch is careful and the skills are registered. What was missing is the
    one thing no test here exercised, because the tests call `_dispatch`
    directly with a context they build themselves.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "verb", None) is None:
        parser.print_help()
        return 2
    from axiom.infra.skills import SkillRegistry
    from axiom.infra.paths import get_user_state_dir

    from .skills import register

    registry = SkillRegistry()
    register(registry)
    ctx = SkillContext(
        registry=registry,
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.lane"),
    )
    return _dispatch(args, ctx)


def _dispatch(args: argparse.Namespace, ctx: SkillContext) -> int:
    """Dispatch through the chokepoint, never straight at the skill.

    This called ``args.fn(params, ctx)``, which is the first of the four
    failures that cost a live credential on ``axi vault renew``: a verb
    reached directly produces no authority decision, no action audit and no
    capability telemetry, so nothing downstream can see it ran. The verbs
    were already registered — ``lane.claim`` and the rest — so the only
    thing missing was asking for them by name.

    ``args.fn`` stays on the parser as the fallback for a context with no
    registry (a bare parse in a test, a container without the extension
    loaded). It is the exception, and it says so, rather than being the
    normal path with the gate as an afterthought.
    """
    params = {
        k: val
        for k, val in vars(args).items()
        if k not in ("fn", "verb", "json") and val not in (None, [], "")
    }
    registry = getattr(ctx, "registry", None)
    if registry is None:
        result = args.fn(params, ctx)
    else:
        try:
            result = invoke_capability(
                registry, f"lane.{args.verb}", params, ctx, surface=CLI_SURFACE
            )
        except KeyError:
            # Registered nowhere is failure #2 of the same incident. Say which
            # verb, rather than falling back silently and hiding it.
            return _fail(f"lane.{args.verb} is not a registered capability")
    out = (
        json.dumps(result.value, indent=2, sort_keys=True)
        if getattr(args, "json", False)
        else _render(args.verb, result)
    )
    print(
        out if result.ok else "\n".join(result.errors), file=sys.stdout if result.ok else sys.stderr
    )
    return result.exit_code
