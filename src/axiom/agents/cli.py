# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI commands for agent learning framework.

Usage:
    axi agents patterns [--agent rivet]    Show learned patterns for an agent
    axi agents patterns --all               Show all patterns across all agents
    axi agents verify <pattern_id>          Mark a pattern as verified
    axi agents promote <pattern_id>         Promote a local pattern to repo
"""

from __future__ import annotations

import argparse
import json
import sys

from axiom.agents.learning import (
    AgentKnowledgeStore,
    Confidence,
    load_all_agent_patterns,
)


def _color(text: str, code: str) -> str:
    """ANSI color wrapper."""
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def _confidence_badge(c: Confidence) -> str:
    badges = {
        Confidence.GREEN: _color("GREEN", "32"),
        Confidence.YELLOW: _color("YELLOW", "33"),
        Confidence.RED: _color("RED", "31"),
    }
    return badges.get(c, c.value)


def cmd_patterns(args: argparse.Namespace) -> None:
    """Show learned patterns."""
    if args.all:
        all_patterns = load_all_agent_patterns()
        if not all_patterns:
            print("No agent patterns found.")
            return
        for agent_name, patterns in all_patterns.items():
            print(f"\n{'=' * 60}")
            print(f"  {agent_name.upper()} ({len(patterns)} patterns)")
            print(f"{'=' * 60}")
            for p in patterns:
                print(f"  [{_confidence_badge(p.confidence)}] {p.pattern_id}")
                print(f"    {p.description}")
                print(f"    verified={p.verified_count} failed={p.failed_count}")
                print()
    elif args.agent:
        store = AgentKnowledgeStore(args.agent)
        patterns = store.load()
        if not patterns:
            print(f"No patterns for agent '{args.agent}'.")
            return
        print(f"\n{args.agent.upper()} — {len(patterns)} patterns\n")
        for p in patterns:
            print(f"  [{_confidence_badge(p.confidence)}] {p.pattern_id}")
            print(f"    {p.description}")
            print(f"    signature: {p.signature}")
            print(f"    diagnosis: {p.diagnosis}")
            print(f"    resolution: {p.resolution}")
            if p.prevention:
                print(f"    prevention: {p.prevention}")
            print(f"    verified={p.verified_count} failed={p.failed_count} maturity={p.maturity}")
            print()
    else:
        print("Specify --agent <name> or --all")

    if args.json:
        if args.all:
            data = {k: [p.to_dict() for p in v] for k, v in load_all_agent_patterns().items()}
        else:
            store = AgentKnowledgeStore(args.agent)
            data = [p.to_dict() for p in store.load()]
        print(json.dumps(data, indent=2))


def cmd_verify(args: argparse.Namespace) -> None:
    """Mark a pattern as verified (success or failure)."""
    # Find which agent owns this pattern
    all_patterns = load_all_agent_patterns()
    for agent_name, patterns in all_patterns.items():
        for p in patterns:
            if p.pattern_id == args.pattern_id:
                store = AgentKnowledgeStore(agent_name)
                success = not args.failed
                store.verify(args.pattern_id, success, args.node or "")
                status = "SUCCESS" if success else "FAILURE"
                print(f"Recorded {status} for {args.pattern_id}")
                return
    print(f"Pattern '{args.pattern_id}' not found.")
    sys.exit(1)


def cmd_promote(args: argparse.Namespace) -> None:
    """Promote a local pattern to the repo."""
    all_patterns = load_all_agent_patterns()
    for agent_name, patterns in all_patterns.items():
        for p in patterns:
            if p.pattern_id == args.pattern_id:
                store = AgentKnowledgeStore(agent_name)
                if store.promote_to_repo(args.pattern_id):
                    print(f"Promoted {args.pattern_id} to repo.")
                else:
                    print(f"Failed to promote {args.pattern_id}.")
                    sys.exit(1)
                return
    print(f"Pattern '{args.pattern_id}' not found.")
    sys.exit(1)


def build_parser(
    subparsers: argparse._SubParsersAction | None = None,
) -> argparse.ArgumentParser:
    """Build the agents CLI parser."""
    if subparsers is not None:
        parser = subparsers.add_parser("agents", help="Agent learning framework")
    else:
        parser = argparse.ArgumentParser(prog="axi agents", description="Agent learning framework")

    sub = parser.add_subparsers(dest="agents_command")

    # patterns
    pat = sub.add_parser("patterns", help="Show learned patterns")
    pat.add_argument("--agent", "-a", help="Agent name (rivet, secur-t, scan, tidy)")
    pat.add_argument("--all", action="store_true", help="Show all agents")
    pat.add_argument("--json", action="store_true", help="JSON output")
    pat.set_defaults(func=cmd_patterns)

    # verify
    ver = sub.add_parser("verify", help="Record verification result")
    ver.add_argument("pattern_id", help="Pattern ID to verify")
    ver.add_argument("--failed", action="store_true", help="Record as failure")
    ver.add_argument("--node", help="Node ID recording the verification")
    ver.set_defaults(func=cmd_verify)

    # promote
    prom = sub.add_parser("promote", help="Promote local pattern to repo")
    prom.add_argument("pattern_id", help="Pattern ID to promote")
    prom.set_defaults(func=cmd_promote)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()
