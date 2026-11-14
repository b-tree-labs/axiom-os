# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``features``: what a node runs, at the command line (ADR-056, ADR-164, ADR-254 amended 2026-11-12).

    features                              what this node is for, and what runs on it
    features list [--kind K]              one kind: extension | function
    features enable <name>                turn on a function, command, extension or background-agents
    features disable <name>               turn it off again
    features set <name> on|off [--kind K] the same at node scope, by value
    features clear <name>                 back to its default
    features role <name>                  give this node a named role; `role clear` undoes it
    features agents <policy>              the site's agent policy: none, assist or local
    features maintenance <level>          how much remote maintenance this site accepts
    features record-of-truth <upstream|local>  which copy of the site's data wins (ADR-180)

Feature flags are ``flags`` (list, resolve, set, explain, kill, cohorts, rollouts, stage
plans, approvals); both nouns read and write the one registry. A flag verb given to
``features`` is refused with the ``flags`` command to run instead.

Thin argparse over the ``features.*`` skills: every verb goes through
``invoke_capability``, so a write is gated, audited and measured exactly as it
is from MCP or the web.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from axiom.infra.cli_format import Column, table
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext


def _cli_name() -> str:
    try:
        from axiom.infra.branding import get_branding

        return (get_branding().cli_name or "axi").strip()
    except Exception:  # noqa: BLE001
        return "axi"


#: The flag verbs ``features`` carried before the split; each is now ``flags <verb>``.
MOVED_TO_FLAGS = ("resolve", "explain", "kill", "cohort", "rollout", "stage", "approve", "changes", "cancel", "tick")


def build_parser() -> argparse.ArgumentParser:
    cli = _cli_name()
    p = argparse.ArgumentParser(
        prog=f"{cli} features",
        description=(
            "what this node runs: its role, functions, optional extensions, agents and maintenance "
            f"policies (feature flags are `{cli} flags`)"
        ),
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="verb")

    ls = sub.add_parser("list", help="what this node runs, or one kind of it (the default verb)")
    ls.add_argument("--kind", choices=("extension", "function", "flag"), default=None,
                    help=f"extension or function (flag points at `{cli} flags list`)")

    en = sub.add_parser("enable", help="turn on a function, a command, an optional extension or background-agents")
    en.add_argument("name")
    dis = sub.add_parser("disable", help="turn off something you turned on")
    dis.add_argument("name")

    st = sub.add_parser("set", help="turn an extension or function on or off on this node")
    st.add_argument("key")
    st.add_argument("value", help="on | off | clear")
    cl = sub.add_parser("clear", help="put an extension or function back to its default")
    cl.add_argument("key")
    for sp in (st, cl):
        sp.add_argument("--scope", default="node", choices=("node", "account", "user"),
                        help="node: what runs is set on the node itself")
        sp.add_argument("--id", dest="scope_id", default="", help="this node's name")
        sp.add_argument("--kind", choices=("extension", "function"), default=None,
                        help="when a name is both an extension and a function")

    ag = sub.add_parser("agents", help="declare this site's agent policy: none, assist or local")
    ag.add_argument("policy", choices=["none", "assist", "local"])
    mt = sub.add_parser("maintenance", help="declare how much remote maintenance this site accepts: off, requests or sessions")
    mt.add_argument("level", choices=["off", "requests", "sessions"])
    rt = sub.add_parser(
        "record-of-truth",
        help="declare which copy of this site's data wins: upstream (contributor) or local (local-first)",
    )
    rt.add_argument("truth", choices=["upstream", "local"])
    ro = sub.add_parser("role", help="give this node a named role, or 'clear' to show everything")
    ro.add_argument("name")
    ro.add_argument(
        "--set", action="append", default=[], metavar="KEY=VALUE",
        help="a setting the role reads, e.g. --set config=/srv/site/site.toml",
    )
    return p


_STATE = {
    "role": "on (role)",
    "feature": "on",
    "always": "on",
    "in-use": "on (in use)",
    "extension-off": "off (extension off)",
    "off": "off",
    "": "off",
}


def _state(row: dict[str, Any]) -> str:
    if row["source"] in _STATE:
        return _STATE[row["source"]]
    return row["value"] if row["kind"] == "flag" or row["subkind"] == "policy" else ("on" if row["enabled"] else "off")


def _flags_line(pointer: dict[str, Any]) -> str:
    count = int(pointer.get("count") or 0)
    command = pointer.get("command") or f"{_cli_name()} flags list"
    noun = "feature flag is" if count == 1 else "feature flags are"
    noun_cmd = command.rsplit(" ", 1)[0]
    return f"{count} {noun} declared: `{command}` lists them, and `{noun_cmd}` sets and explains them."


def render(value: dict[str, Any], *, kind: str | None = None) -> str:
    cli = _cli_name()
    rows = value.get("rows") or []
    lines: list[str] = []
    if value.get("problem"):
        lines.append(f"note: {value['problem']}")
    pointer = value.get("flags") or {}
    if kind == "flag":
        lines.append(_flags_line(pointer))
        return "\n".join(lines).rstrip()
    if kind is None:
        if value.get("confined"):
            lines.append(
                f"This node's role: {value.get('role') or 'node'} ({', '.join(value['functions'])})."
            )
            lines.append("Only its commands show. Anything below marked off can be turned on.")
        else:
            lines.append("This node has no role, so every command shows.")
            if value.get("roles"):
                lines.append(
                    f"Give it one with `{cli} features role <name>`: "
                    + ", ".join(sorted(value["roles"]))
                )
        if value.get("agent_policy"):
            lines.append(f"Agents: {value['agent_policy']} (site policy). Change it with `{cli} features agents <none|assist|local>`.")
        if value.get("record_of_truth"):
            lines.append(f"Record of truth: {value['record_of_truth']} (ADR-180). Change it with "
                         f"`{cli} features record-of-truth <upstream|local>`.")
        if value.get("maintenance"):
            lines.append(f"Remote maintenance: {value['maintenance']} (site policy). Change it with "
                         f"`{cli} features maintenance <off|requests|sessions>`.")
        lines.append("")
    groups = (
        ("extension", None, "Extensions (node scope)"),
        ("function", "node-function", "Functions (node scope)"),
        ("function", "command", "Other commands (node scope)"),
        ("function", "service", "Services (node scope)"),
        ("function", "policy", "Site policies"),
    )
    for k, sub, heading in groups:
        group = [r for r in rows if r["kind"] == k and (sub is None or r["subkind"] == sub)]
        if not group:
            continue
        lines.append(f"{heading}:")
        cells = []
        for r in group:
            extra = ""
            if r["subkind"] == "node-function" and r["commands"]:
                extra = ", ".join(r["commands"])
            elif r["subkind"] == "service":
                extra = "scheduled upkeep services; off on a role node until turned on"
            elif r["subkind"] == "policy":
                extra = r.get("description") or ""
            elif r["subkind"] == "command" and r["source"] == "extension-off":
                extra = f"from {r['owner']}, which is off"
            elif r["kind"] == "extension":
                extra = r.get("description") or ""
            cells.append((r["name"], _state(r), extra))
        lines.extend(table(cells, [Column(), Column(), Column(wrap=True)]))
        lines.append("")
    if kind is None and pointer:
        lines.append(_flags_line(pointer))
        lines.append("")
    for line in value.get("drift") or []:
        lines.append(f"note: {line}")
    if kind is None and value.get("confined"):
        lines.append(f"Turn one on: {cli} features enable <name>   ·   undo: {cli} features disable <name>")
        lines.append(f"Show everything again: {cli} features role clear")
    return "\n".join(lines).rstrip()


def context() -> SkillContext:
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillRegistry

    from .skills import bind

    registry = SkillRegistry()
    bind(registry)
    import sys

    # A person at a terminal answers the prompt a person-only flag asks (#1811); a script does not.
    return SkillContext(
        registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.features"),
        user_prompt=input if sys.stdin.isatty() else None,
    )


def run_capability(capability: str, params: dict[str, Any]):
    ctx = context()
    return invoke_capability(ctx.registry, capability, params, ctx, surface=CLI_SURFACE)


_VERBS = {
    "list": "features.list",
    "enable": "features.enable",
    "disable": "features.disable",
    "set": "features.set",
    "clear": "features.set",
    "role": "features.role",
    "agents": "features.agents",
    "maintenance": "features.maintenance",
    "record-of-truth": "features.record_of_truth",
}


def moved_verb(argv: list[str]) -> str | None:
    """The flag verb ``argv`` names, if any: it moved to ``flags``."""
    words = [a for a in argv if not a.startswith("-")]
    return words[0] if words and words[0] in MOVED_TO_FLAGS else None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    moved = moved_verb(argv)
    if moved is not None:
        cli = _cli_name()
        print(f"`{cli} features {moved}` is a feature-flag verb; it is `{cli} flags {moved}`. "
              f"`{cli} features` covers what this node runs.", file=sys.stderr)
        return 2
    args = build_parser().parse_args(argv)
    verb = args.verb or "list"
    params: dict[str, Any] = {}
    if verb in ("enable", "disable", "role"):
        params["name"] = args.name
    kind = getattr(args, "kind", None)
    if verb == "list":
        params = {"kind": kind}
    if verb in ("set", "clear"):
        params = {
            "key": args.key,
            "value": args.value if verb == "set" else "clear",
            "scope": args.scope,
            "scope_id": args.scope_id,
            "kind": args.kind,
        }
    if verb == "agents":
        params["policy"] = args.policy
    if verb == "maintenance":
        params["level"] = args.level
    if verb == "record-of-truth":
        params["truth"] = args.truth
    if verb == "role":
        settings = {}
        for item in args.set:
            key, sep, val = item.partition("=")
            if not sep or not key:
                print(f"--set wants KEY=VALUE, got {item!r}", file=sys.stderr)
                return 2
            settings[key.strip()] = val.strip()
        params["settings"] = settings
    result = run_capability(_VERBS[verb], params)
    if not result.ok:
        print("\n".join(result.errors), file=sys.stderr)
        return result.exit_code or 1
    if args.json:
        print(json.dumps(result.value, indent=2, default=str))
        return 0
    for action in result.actions_taken:
        print(f"• {action}")
    if verb not in ("set", "clear"):
        print(render(result.value, kind=kind if verb == "list" else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
