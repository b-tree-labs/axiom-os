# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``<cli> instrument``: list and check every instrument, and reach each kind's own verbs.

    axi instrument list [--kinds] [--json]
    axi instrument status [--json]
    axi instrument <kind> <verb> ...        # the kind's own command line

The noun's own verbs are thin wrappers (ADR-056): flags become a params dict and go
to skill ``instrument.<verb>`` through ``invoke_capability``. Any other first word
is a registered kind, and the rest of the command line is the kind's: its verbs,
flags, output and exit code (ADR-256 D4).

``SKILL_FOR_VERB`` is the map ADR-243 introduced for a noun whose second word is a
registered name: ``{"list": "instrument.list", "<kind> <verb>": "<kind's skill>"}``,
read by ``cli/command_space.py`` before it joins words, so help, Tab and the
``next:`` lines know which skill each verb wraps (spec-instrument INS-P1).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

#: The noun's own verbs, in the order help lists them.
NOUN_VERBS = ("list", "status")

_HELP = ("-h", "--help")
_PACKAGE = "axiom.extensions.builtins.instrument"


def skill_for(verb: str) -> str:
    return f"instrument.{verb}"


def _registry():
    from .. import registry

    return registry


def _prog() -> str:
    from ..naming import noun_prog

    return noun_prog()


def _common() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print the result envelope as JSON")
    return common


def _add_noun_verbs(sub: Any) -> None:
    common = _common()
    ls = sub.add_parser(
        "list",
        parents=[common],
        help="every instrument of every kind, or the registered kinds",
        description="Every instrument this node has, of every instrument kind.",
    )
    ls.add_argument("--kinds", action="store_true", help="list the registered instrument kinds instead")
    sub.add_parser(
        "status",
        parents=[common],
        help="one summary per instrument kind",
        description="One summary per instrument kind: whether it answered, and its instruments by health.",
    )


def _noun_parser(prog: str) -> argparse.ArgumentParser:
    """The noun's own verbs only: what ``list`` and ``status`` are parsed with."""
    parser = argparse.ArgumentParser(prog=prog)
    _add_noun_verbs(parser.add_subparsers(dest="verb"))
    return parser


def get_parser(prog: str | None = None) -> argparse.ArgumentParser:
    """The noun's verbs and one sub-command per registered kind, from the kind's parser.

    Used for ``--help`` and Tab. Dispatch reads the first word itself, so a kind's
    command line is the kind's to parse.
    """
    prog = prog or _prog()
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Instruments: what this node quantifies from its sensors. "
            f"Each kind's own verbs are `{prog} <kind> <verb>`."
        ),
    )
    kinds = _registry().catalog().kinds()
    metavar = "{" + ",".join([*NOUN_VERBS, *(k.name for k in kinds)]) + "}"
    sub = parser.add_subparsers(dest="verb", metavar=metavar)
    _add_noun_verbs(sub)
    for kind in kinds:
        try:
            own = kind.build_parser(f"{prog} {kind.name}")
        except Exception as exc:  # noqa: BLE001 - one kind's parser never hides the others
            logging.getLogger(__name__).warning("instrument kind %r: no parser: %s", kind.name, exc)
            sub.add_parser(kind.name, help=kind.description)
            continue
        sub.add_parser(
            kind.name,
            parents=[own],
            add_help=False,
            help=f"{kind.description} (yields {kind.yields})",
            description=own.description,
        )
    return parser


def skill_for_verb() -> dict[str, str]:
    """``SKILL_FOR_VERB`` for the kinds registered now."""
    out = {verb: skill_for(verb) for verb in NOUN_VERBS}
    for kind in _registry().catalog().kinds():
        for verb, skill in dict(getattr(kind, "skill_for_verb", {}) or {}).items():
            out[f"{kind.name} {verb}"] = str(skill)
    return out


def parser_sources() -> list[str]:
    """The source files this noun's parser is assembled from (INS-P2).

    The noun's own modules and each registered kind's, as loaded now, so the Tab
    cache notices when one of them changes.
    """
    packages = {_PACKAGE}
    for kind in _registry().catalog().kinds():
        module = type(kind).__module__
        packages.add(module.rpartition(".")[0] or module)
    found: set[str] = set()
    for name, mod in list(sys.modules.items()):
        if not any(name == p or name.startswith(p + ".") for p in packages):
            continue
        path = getattr(mod, "__file__", None)
        if path:
            found.add(str(path))
    return sorted(found)


# ---- running ------------------------------------------------------------------


def _invoke(capability: str, params: dict[str, Any]):
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
    from axiom.infra.skills import SkillContext, SkillRegistry

    from ..skills import bind

    registry = SkillRegistry()
    bind(registry)
    ctx = SkillContext(
        registry=registry,
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.instrument"),
    )
    return invoke_capability(registry, capability, params, ctx, surface=CLI_SURFACE)


def _run_noun_verb(argv: list[str], prog: str) -> int:
    args = _noun_parser(prog).parse_args(argv)
    params: dict[str, Any] = {"kinds": True} if getattr(args, "kinds", False) else {}
    result = _invoke(skill_for(args.verb), params)
    if args.json:
        envelope = {
            "ok": result.ok,
            "value": result.value,
            "errors": result.errors,
            "actions_taken": result.actions_taken,
        }
        if result.unmet:
            envelope["unmet"] = result.unmet
        print(json.dumps(envelope, indent=2, default=str))
        return 0 if result.ok else 1
    if result.ok or result.value:
        _render(args.verb, result.value or {}, prog)
    for error in result.errors:
        print(f"error: {error}", file=sys.stderr)
    return 0 if result.ok else 1


def _refuse_unknown(word: str, prog: str) -> int:
    cat = _registry().catalog()
    known = ", ".join(cat.names()) or "none"
    print(f"{prog}: no instrument kind named {word!r} (registered: {known}).", file=sys.stderr)
    near = cat.nearest(word)
    if near:
        print(f"\nDid you mean: {prog} {near}?", file=sys.stderr)
    print(f"\n`{prog} list --kinds` lists the kinds this node has.", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    prog = _prog()
    if not args:
        get_parser(prog).print_help()
        return 2
    head, rest = args[0], args[1:]
    if head in _HELP:
        get_parser(prog).parse_args([head])  # prints help and exits 0
        return 0
    if head == "help":
        get_parser(prog).print_help()
        return 0
    if head.startswith("-"):
        _noun_parser(prog).print_usage(sys.stderr)
        print(f"{prog}: error: {head!r} is not a verb or a kind; `{prog} --help` lists them", file=sys.stderr)
        return 2
    if head in NOUN_VERBS:
        return _run_noun_verb(args, prog)

    registry = _registry()
    try:
        kind = registry.catalog().kind(head)
    except registry.UnknownKind:
        return _refuse_unknown(head, prog)
    except registry.InstrumentKindError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        if exc.fix:
            print(f"  fix: {exc.fix}", file=sys.stderr)
        return 1
    rc = kind.main(rest, f"{prog} {head}")
    return rc if isinstance(rc, int) and not isinstance(rc, bool) else (0 if rc in (None, True) else 1)


# ---- rendering ----------------------------------------------------------------


def _table(rows: list[tuple[str, ...]], headers: tuple[str, ...], wrap_last: bool = True) -> None:
    from axiom.infra.cli_format import Column, table, terminal_width

    columns = [Column(h) for h in headers[:-1]] + [Column(headers[-1], wrap=wrap_last)]
    for line in table(rows, columns, width=terminal_width(reserve=2), headers=True):
        print(line)


def _render(verb: str, value: dict[str, Any], prog: str) -> None:
    if verb == "list" and "kinds" in value:
        rows = value["kinds"]
        if not rows:
            print("(no instrument kinds are registered)")
            return
        _table(
            [
                (
                    r["name"],
                    r.get("yields") or "",
                    r["state"],
                    r.get("origin") or "",
                    (r.get("description") if r["state"] == "registered" else r.get("reason")) or "",
                )
                for r in rows
            ],
            ("kind", "yields", "state", "origin", "what it is"),
        )
        return
    if verb == "list":
        rows = value.get("instruments") or []
        if rows:
            _table(
                [(r["kind"], r["name"], r["health"], r["state"], r.get("detail") or "") for r in rows],
                ("kind", "name", "health", "state", "detail"),
            )
        elif not value.get("failed"):
            if value.get("registered"):
                print(f"(no instruments are declared; each kind's own verbs declare them: `{prog} <kind> --help`)")
            else:
                print(f"(no instrument kinds are registered; `{prog} list --kinds` shows why)")
        for failed in value.get("failed") or []:
            print(f"{failed['kind']}: did not answer: {failed['error']}")
            if failed.get("fix"):
                print(f"  fix: {failed['fix']}")
        return
    if verb == "status":
        summaries = value.get("kinds") or []
        if not summaries:
            print(f"(no instrument kinds are registered; `{prog} list --kinds` shows why)")
            return
        for s in summaries:
            if s["ok"]:
                print(f"{s['kind']}: {s['summary']}")
            else:
                print(f"{s['kind']}: not ok: {s.get('error') or s['summary']}")
                if s.get("fix"):
                    print(f"  fix: {s['fix']}")


def __getattr__(name: str) -> Any:
    if name == "SKILL_FOR_VERB":
        return skill_for_verb()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


cli = main

__all__ = ["NOUN_VERBS", "get_parser", "main", "parser_sources", "skill_for", "skill_for_verb"]
