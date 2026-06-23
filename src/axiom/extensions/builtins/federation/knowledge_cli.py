# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI handler for ``axi knowledge`` — knowledge observatory."""

from __future__ import annotations

import argparse
import json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi knowledge",
        description="Knowledge observatory — track velocity, accumulation, and impact",
    )
    sub = parser.add_subparsers(dest="action")

    status_p = sub.add_parser("status", help="Show knowledge health dashboard")
    status_p.add_argument("--json", action="store_true", help="Output as JSON")

    velocity_p = sub.add_parser("velocity", help="Knowledge ingestion rate")
    velocity_p.add_argument("--json", action="store_true", help="Output as JSON")

    accumulation_p = sub.add_parser("accumulation", help="What do we know?")
    accumulation_p.add_argument("--json", action="store_true", help="Output as JSON")

    impact_p = sub.add_parser("impact", help="Is knowledge being used?")
    impact_p.add_argument("--json", action="store_true", help="Output as JSON")

    report_p = sub.add_parser("report", help="Generate full knowledge report")
    report_p.add_argument("--period", type=int, default=30, help="Period in days")
    report_p.add_argument("--format", choices=["human", "json"], default="human")

    gaps_p = sub.add_parser("gaps", help="Show knowledge coverage gaps")
    gaps_p.add_argument("--format", choices=["human", "json"], default="human")

    saved_p = sub.add_parser("saved", help="List saved findings from chat (/save)")
    saved_p.add_argument("--domain", help="Filter by domain")
    saved_p.add_argument("--since", help="Filter by date (ISO 8601)")
    saved_p.add_argument("query", nargs="?", help="Search saved items")
    saved_p.add_argument("--format", choices=["human", "json"], default="human")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.action:
        parser.print_help()
        return 1
    handlers = {
        "status": _cmd_status,
        "velocity": _cmd_velocity,
        "accumulation": _cmd_accumulation,
        "impact": _cmd_impact,
        "report": _cmd_report,
        "gaps": _cmd_gaps,
        "saved": _cmd_saved,
    }
    return handlers[args.action](args)


def _cmd_status(args: argparse.Namespace) -> int:
    from axiom.vega.federation.knowledge_metrics import KnowledgeMetricsService

    svc = KnowledgeMetricsService()
    report = svc.generate_report()
    d = report.to_dict()
    if getattr(args, "json", False):
        print(json.dumps(d, indent=2))
        return 0
    v = d["velocity"]
    a = d["accumulation"]
    i = d["impact"]
    print("Knowledge Observatory")
    print("=" * 50)
    print("\nVelocity (last 30 days):")
    print(f"  Facts/day:        {v['facts_per_day']}")
    print(f"  Promotion rate:   {v['promotion_rate']:.0%}")
    print("\nAccumulation:")
    print(f"  Total facts:      {a['total_facts']}")
    if a["coverage_gaps"]:
        print(f"  Coverage gaps:    {', '.join(a['coverage_gaps'])}")
    print("\nImpact:")
    print(f"  Retrievals/day:   {i['retrievals_per_day']}")
    print(
        f"  Federation-only:  {i['federation_unique_answers']} answers only possible via federation"
    )
    print(f"  Self-sufficiency: {i['self_sufficiency_rate']:.0%}")
    return 0


def _cmd_velocity(args: argparse.Namespace) -> int:
    from axiom.vega.federation.knowledge_metrics import KnowledgeMetricsService

    svc = KnowledgeMetricsService()
    v = svc.compute_velocity()
    d = v.to_dict()
    if getattr(args, "json", False):
        print(json.dumps(d, indent=2))
    else:
        print(f"Facts/day:      {d.get('facts_per_day', 0)}")
        print(f"Promotion rate: {d.get('promotion_rate', 0):.0%}")
    return 0


def _cmd_accumulation(args: argparse.Namespace) -> int:
    from axiom.vega.federation.knowledge_metrics import KnowledgeMetricsService

    svc = KnowledgeMetricsService()
    a = svc.compute_accumulation()
    d = a.to_dict()
    if getattr(args, "json", False):
        print(json.dumps(d, indent=2))
    else:
        print(f"Total facts:    {d.get('total_facts', 0)}")
        gaps = d.get("coverage_gaps", [])
        if gaps:
            print(f"Coverage gaps:  {', '.join(gaps)}")
    return 0


def _cmd_impact(args: argparse.Namespace) -> int:
    from axiom.vega.federation.knowledge_metrics import KnowledgeMetricsService

    svc = KnowledgeMetricsService()
    i = svc.compute_impact()
    d = i.to_dict()
    if getattr(args, "json", False):
        print(json.dumps(d, indent=2))
    else:
        print(f"Retrievals/day:   {d.get('retrievals_per_day', 0)}")
        print(f"Federation-only:  {d.get('federation_unique_answers', 0)}")
        print(f"Self-sufficiency: {d.get('self_sufficiency_rate', 0):.0%}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from axiom.vega.federation.knowledge_metrics import KnowledgeMetricsService

    svc = KnowledgeMetricsService()
    report = svc.generate_report()
    fmt = getattr(args, "format", "human")
    if fmt == "json":
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _cmd_status(args)
    return 0


def _cmd_saved(args: argparse.Namespace) -> int:
    from axiom.vega.federation.knowledge_metrics import KnowledgeMetricsService

    svc = KnowledgeMetricsService()
    events = svc._load_events(period_days=36500)  # all time

    # Filter to saved facts only
    saved = [e for e in events if e.get("type") == "fact_added" and e.get("source") == "chat_save"]

    # Apply filters
    domain = getattr(args, "domain", None)
    if domain:
        saved = [e for e in saved if e.get("domain") == domain]

    since = getattr(args, "since", None)
    if since:
        saved = [e for e in saved if e.get("timestamp", "") >= since]

    query = getattr(args, "query", None)
    if query:
        q = query.lower()
        saved = [e for e in saved if q in e.get("content", "").lower()]

    fmt = getattr(args, "format", "human")
    if fmt == "json":
        print(json.dumps(saved, indent=2))
    elif not saved:
        print("No saved findings yet. Use /save in chat to bookmark insights.")
    else:
        print(f"Saved findings ({len(saved)}):\n")
        for e in saved:
            ts = e.get("timestamp", "")[:10]
            content = e.get("content", "")[:80]
            domain_tag = e.get("domain", "")
            print(f"  \u2605 [{ts}] {content}")
            if domain_tag:
                print(f"    domain: {domain_tag}")
            print()
    return 0


def _cmd_gaps(args: argparse.Namespace) -> int:
    from axiom.vega.federation.knowledge_metrics import KnowledgeMetricsService

    svc = KnowledgeMetricsService()
    a = svc.compute_accumulation()
    fmt = getattr(args, "format", "human")
    if fmt == "json":
        print(json.dumps({"coverage_gaps": a.coverage_gaps, "by_domain": a.by_domain}, indent=2))
    elif not a.coverage_gaps:
        print("No coverage gaps detected.")
    else:
        print("Knowledge Coverage Gaps:")
        for gap in a.coverage_gaps:
            count = a.by_domain.get(gap, 0)
            print(f"  {gap}: {count} facts (sparse)")
    return 0
