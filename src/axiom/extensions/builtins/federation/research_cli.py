# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI handler for ``axi research`` — Call to Research management."""

from __future__ import annotations

import argparse
import json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi research",
        description="Call to Research — distributed research coordination",
    )
    sub = parser.add_subparsers(dest="action")

    create_p = sub.add_parser("create", help="Create a Call to Research")
    create_p.add_argument("title", help="Title of the research call")
    create_p.add_argument("-d", "--description", default="", help="Description")
    create_p.add_argument("-l", "--level", type=int, default=1, choices=[1, 2, 3, 4, 5])
    create_p.add_argument("-s", "--scope", default="consortium")
    create_p.add_argument("--tags", nargs="*", default=[])
    create_p.add_argument("--json", action="store_true", help="Output as JSON")

    list_p = sub.add_parser("list", help="List calls")
    list_p.add_argument("--status", default=None, help="Filter by status")
    list_p.add_argument("--level", type=int, default=None)
    list_p.add_argument("--format", choices=["human", "json"], default="human")

    show_p = sub.add_parser("show", help="Show call details")
    show_p.add_argument("call_id", help="Call ID")
    show_p.add_argument("--format", choices=["human", "json"], default="human")

    claim_p = sub.add_parser("claim", help="Claim a research part")
    claim_p.add_argument("call_id", help="Call ID")
    claim_p.add_argument("part_id", help="Part ID")
    claim_p.add_argument("--node-id", default="local", help="Claiming node ID")
    claim_p.add_argument("--name", default="Local Operator", help="Claimer name")
    claim_p.add_argument("--json", action="store_true", help="Output as JSON")

    submit_p = sub.add_parser("submit", help="Submit response to a part")
    submit_p.add_argument("call_id", help="Call ID")
    submit_p.add_argument("part_id", help="Part ID")
    submit_p.add_argument("--content", required=True, help="JSON content")
    submit_p.add_argument("--provenance", nargs="*", default=[])
    submit_p.add_argument("--json", action="store_true", help="Output as JSON")

    publish_p = sub.add_parser("publish", help="Publish synthesis")
    publish_p.add_argument("call_id", help="Call ID")
    publish_p.add_argument("--synthesis", required=True, help="Synthesis text")
    publish_p.add_argument("--json", action="store_true", help="Output as JSON")

    chain_p = sub.add_parser("chain", help="Show research chain (linked calls)")
    chain_p.add_argument("call_id", help="Call ID")
    chain_p.add_argument("--format", choices=["human", "json"], default="human")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.action:
        parser.print_help()
        return 1
    handlers = {
        "create": _cmd_create,
        "list": _cmd_list,
        "show": _cmd_show,
        "claim": _cmd_claim,
        "submit": _cmd_submit,
        "publish": _cmd_publish,
        "chain": _cmd_chain,
    }
    return handlers[args.action](args)


def _cmd_create(args: argparse.Namespace) -> int:
    from axiom.vega.federation.research import ResearchService

    svc = ResearchService()
    call = svc.create_call(
        title=args.title,
        description=args.description,
        caller_node_id="local",
        caller_name="Local Operator",
        level=args.level,
        scope=args.scope,
        tags=args.tags,
    )
    data = {
        "call_id": call.call_id,
        "title": call.title,
        "level": call.level.value,
        "level_name": call.level.name,
        "status": call.status.value,
    }
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
    else:
        print(f"Created: {call.call_id}")
        print(f"  Title: {call.title}")
        print(f"  Level: {call.level.value} ({call.level.name})")
        print(f"  Status: {call.status.value}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    from axiom.vega.federation.research import ResearchService

    svc = ResearchService()
    calls = svc.list_calls(status=args.status, level=args.level)
    fmt = getattr(args, "format", "human")
    if fmt == "json":
        print(json.dumps([c.to_dict() for c in calls], indent=2))
    else:
        if not calls:
            print("No research calls found.")
            return 0
        for c in calls:
            parts_info = f"{c.to_dict()['parts_complete']}/{c.to_dict()['parts_total']}"
            print(
                f"  {c.call_id}  L{c.level.value}  {c.status.value:<12s}  {parts_info}  {c.title}"
            )
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    from axiom.vega.federation.research import ResearchService

    svc = ResearchService()
    call = svc.get(args.call_id)
    if call is None:
        print(f"Call not found: {args.call_id}")
        return 1
    fmt = getattr(args, "format", "human")
    if fmt == "json":
        print(json.dumps(call.to_dict(), indent=2))
    else:
        d = call.to_dict()
        print(f"Call: {d['call_id']}")
        print(f"  Title:   {d['title']}")
        print(f"  Level:   {d['level']}")
        print(f"  Status:  {d['status']}")
        print(f"  Parts:   {d['parts_complete']}/{d['parts_total']}")
        if call.input_from:
            print(f"  Input from: {', '.join(call.input_from)}")
        if call.output_to:
            print(f"  Output to:  {', '.join(call.output_to)}")
        for p in d["parts"]:
            status_icon = "+" if p["status"] == "accepted" else "-"
            print(f"    [{status_icon}] {p['part_id']}: {p['description']} ({p['status']})")
    return 0


def _cmd_claim(args: argparse.Namespace) -> int:
    from axiom.vega.federation.research import ResearchService

    svc = ResearchService()
    try:
        part = svc.claim_part(args.call_id, args.part_id, args.node_id, args.name)
        data = {"part_id": part.part_id, "assigned_name": part.assigned_name}
        if getattr(args, "json", False):
            print(json.dumps(data, indent=2))
        else:
            print(f"Claimed: {part.part_id} -> {part.assigned_name}")
    except ValueError as e:
        print(f"Error: {e}")
        return 1
    return 0


def _cmd_submit(args: argparse.Namespace) -> int:
    from axiom.vega.federation.research import ResearchService

    svc = ResearchService()
    try:
        content = json.loads(args.content)
    except json.JSONDecodeError:
        print("Error: --content must be valid JSON")
        return 1
    try:
        resp = svc.submit_response(
            args.call_id, args.part_id, content=content, provenance=args.provenance
        )
        data = {"part_id": resp.part_id, "status": "submitted"}
        if getattr(args, "json", False):
            print(json.dumps(data, indent=2))
        else:
            print(f"Submitted response for {resp.part_id}")
    except ValueError as e:
        print(f"Error: {e}")
        return 1
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    from axiom.vega.federation.research import ResearchService

    svc = ResearchService()
    try:
        call = svc.publish_synthesis(args.call_id, synthesis=args.synthesis)
        data = {"call_id": call.call_id, "status": call.status.value}
        if getattr(args, "json", False):
            print(json.dumps(data, indent=2))
        else:
            print(f"Published: {call.call_id} -> {call.status.value}")
    except ValueError as e:
        print(f"Error: {e}")
        return 1
    return 0


def _cmd_chain(args: argparse.Namespace) -> int:
    from axiom.vega.federation.research import ResearchService

    svc = ResearchService()
    try:
        chain = svc.get_research_chain(args.call_id)
    except ValueError as e:
        print(f"Error: {e}")
        return 1
    fmt = getattr(args, "format", "human")
    if fmt == "json":
        print(json.dumps([c.to_dict() for c in chain], indent=2))
    else:
        print(f"Research chain ({len(chain)} calls):")
        for i, c in enumerate(chain):
            prefix = "  " + ("-> " if i > 0 else "   ")
            print(f"{prefix}{c.call_id}  L{c.level.value}  {c.status.value}  {c.title}")
    return 0
