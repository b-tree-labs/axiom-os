# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI handler for ``axi security`` and ``axi chaos`` — SECUR-T + chaos testing.

Usage:
    axi security status             Show security health
    axi security alerts             List open alerts
    axi security alerts --all       List all alerts
    axi security resolve <id>       Resolve an alert
    axi security trust <node_id>    Show trust score for a node
    axi security rules              List anomaly detection rules
    axi security escalation         Show escalation path health
    axi security scan <node_id>     Run anomaly check on a node

    axi chaos list                  List available chaos scenarios
    axi chaos run <scenario>        Run a specific scenario
    axi chaos run --all             Run all scenarios
    axi chaos status                Show results from last run
"""

from __future__ import annotations

import argparse
import json
import sys

# ---------------------------------------------------------------------------
# Security CLI
# ---------------------------------------------------------------------------


def build_security_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi security",
        description="SECUR-T — federation security guardian",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  axi security status
  axi security alerts --all
  axi security trust node-abc
  axi security scan node-abc
""",
    )

    sub = parser.add_subparsers(dest="action")

    def _add_json(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--json", action="store_true", help="Output as JSON")
        return p

    _add_json(sub.add_parser("status", help="Show security health"))

    alerts_p = _add_json(sub.add_parser("alerts", help="List alerts"))
    alerts_p.add_argument(
        "--all", action="store_true", dest="show_all", help="Show all alerts (not just open)"
    )
    alerts_p.add_argument("--node", help="Filter by node ID")

    resolve_p = _add_json(sub.add_parser("resolve", help="Resolve an alert"))
    resolve_p.add_argument("alert_id", help="Alert ID to resolve")
    resolve_p.add_argument("--false-positive", action="store_true", help="Mark as false positive")

    trust_p = _add_json(sub.add_parser("trust", help="Show trust score"))
    trust_p.add_argument("node_id", help="Node ID")

    _add_json(sub.add_parser("rules", help="List anomaly detection rules"))

    _add_json(sub.add_parser("escalation", help="Show escalation path health"))

    scan_p = _add_json(sub.add_parser("scan", help="Run anomaly check on a node"))
    scan_p.add_argument("node_id", help="Node ID to scan")

    parser.add_argument("--json", action="store_true", help="Output as JSON")

    return parser


def security_main(argv: list[str] | None = None) -> int:
    parser = build_security_parser()
    args = parser.parse_args(argv)

    if not args.action:
        parser.print_help()
        return 0

    handlers = {
        "status": _sec_status,
        "alerts": _sec_alerts,
        "resolve": _sec_resolve,
        "trust": _sec_trust,
        "rules": _sec_rules,
        "escalation": _sec_escalation,
        "scan": _sec_scan,
    }

    handler = handlers.get(args.action)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


def _sec_status(args: argparse.Namespace) -> int:
    from axiom.vega.federation.security import SecurityService

    svc = SecurityService()
    status = svc.get_security_status()
    if getattr(args, "json", False):
        print(json.dumps(status, indent=2))
    else:
        healthy = "HEALTHY" if status["healthy"] else "DEGRADED"
        print(f"Security: {healthy}")
        print(f"  Open alerts:     {status['open_alerts']}")
        print(f"  Total alerts:    {status['total_alerts']}")
        print(f"  Critical:        {status['critical_alerts']}")
        print(f"  Escalation path: {'OK' if status['escalation_path'] else 'NOT CONFIGURED'}")
        print(f"  Anomaly rules:   {status['anomaly_rules']}")
    return 0


def _sec_alerts(args: argparse.Namespace) -> int:
    from axiom.vega.federation.security import SecurityService

    svc = SecurityService()
    status_filter = None if args.show_all else "open"
    alerts = svc.list_alerts(status=status_filter, node_id=getattr(args, "node", None))
    if getattr(args, "json", False):
        print(json.dumps([a.to_dict() for a in alerts], indent=2))
    else:
        if not alerts:
            print("No alerts.")
            return 0
        for a in alerts:
            print(
                f"  [{a.threat_level.value.upper():8s}] {a.alert_id}  {a.rule}  ({a.source_node_id})"
            )
            print(f"           {a.description}")
    return 0


def _sec_resolve(args: argparse.Namespace) -> int:
    from axiom.vega.federation.security import SecurityService

    svc = SecurityService()
    svc.resolve_alert(args.alert_id, resolved_by="operator", false_positive=args.false_positive)
    print(f"Resolved: {args.alert_id}")
    return 0


def _sec_trust(args: argparse.Namespace) -> int:
    from axiom.vega.federation.security import SecurityService

    svc = SecurityService()
    score = svc.get_trust_score(args.node_id)
    if getattr(args, "json", False):
        print(json.dumps(score.to_dict(), indent=2))
    else:
        print(f"Node: {score.node_id}")
        print(f"  Trust score:      {score.score:.3f}")
        print(f"  Content verified: {score.content_verified}")
        print(f"  Content failed:   {score.content_failed}")
        print(f"  Anomalies:        {score.anomalies_detected}")
    return 0


def _sec_rules(args: argparse.Namespace) -> int:
    from axiom.vega.federation.security import SecurityService

    svc = SecurityService()
    rules = svc.list_rules()
    if getattr(args, "json", False):
        print(json.dumps([r.to_dict() for r in rules], indent=2))
    else:
        for r in rules:
            print(f"  {r.name:25s} [{r.threat_level.value:8s}]  {r.description}")
    return 0


def _sec_escalation(args: argparse.Namespace) -> int:
    from axiom.vega.federation.security import SecurityService

    svc = SecurityService()
    status = svc.verify_escalation_path()
    if getattr(args, "json", False):
        print(json.dumps(status, indent=2))
    else:
        healthy = "HEALTHY" if status["healthy"] else "NOT CONFIGURED"
        print(f"Escalation path: {healthy}")
        for c in status.get("contacts", []):
            stale = " (STALE)" if c.get("stale") else ""
            print(f"  {c['name']} <{c['email']}> [{c['role']}]{stale}")
    return 0


def _sec_scan(args: argparse.Namespace) -> int:
    from axiom.vega.federation.security import SecurityService

    svc = SecurityService()
    alerts = svc.check_anomalies(args.node_id)
    if getattr(args, "json", False):
        print(json.dumps([a.to_dict() for a in alerts], indent=2))
    else:
        if not alerts:
            print(f"No anomalies detected for {args.node_id}.")
        else:
            print(f"Anomalies for {args.node_id}:")
            for a in alerts:
                print(f"  [{a.threat_level.value.upper():8s}] {a.rule}: {a.description}")
    return 0


# ---------------------------------------------------------------------------
# Chaos CLI
# ---------------------------------------------------------------------------


def build_chaos_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi chaos",
        description="Chaos test framework — federation resilience testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  axi chaos list
  axi chaos run content-injection
  axi chaos run --all
  axi chaos status
""",
    )

    sub = parser.add_subparsers(dest="action")

    def _add_json(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--json", action="store_true", help="Output as JSON")
        return p

    _add_json(sub.add_parser("list", help="List available chaos scenarios"))

    run_p = _add_json(sub.add_parser("run", help="Run a chaos scenario"))
    run_p.add_argument("scenario", nargs="?", help="Scenario name")
    run_p.add_argument("--all", action="store_true", dest="run_all", help="Run all scenarios")

    _add_json(sub.add_parser("status", help="Show results from last run"))

    parser.add_argument("--json", action="store_true", help="Output as JSON")

    return parser


def chaos_main(argv: list[str] | None = None) -> int:
    parser = build_chaos_parser()
    args = parser.parse_args(argv)

    if not args.action:
        parser.print_help()
        return 0

    handlers = {
        "list": _chaos_list,
        "run": _chaos_run,
        "status": _chaos_status,
    }

    handler = handlers.get(args.action)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


def _chaos_list(args: argparse.Namespace) -> int:
    from axiom.vega.federation.chaos import ChaosRunner

    runner = ChaosRunner()
    scenarios = runner.list_scenarios()
    if getattr(args, "json", False):
        print(json.dumps([s.to_dict() for s in scenarios], indent=2))
    else:
        print("Available chaos scenarios:")
        for s in scenarios:
            print(f"  {s.name:25s} {s.description}")
    return 0


def _chaos_run(args: argparse.Namespace) -> int:
    from axiom.vega.federation.chaos import ChaosRunner

    runner = ChaosRunner()

    if args.run_all:
        results = runner.run_all()
    elif args.scenario:
        results = [runner.run_scenario(args.scenario)]
    else:
        print("Specify a scenario name or --all", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        for r in results:
            status = "PASS" if r.success else "FAIL"
            print(f"  [{status}] {r.scenario}")
            if r.detection_time_ms:
                print(f"         Detection: {r.detection_time_ms:.1f}ms")
            if r.alerts_generated:
                print(f"         Alerts: {r.alerts_generated}")
            if not r.success:
                for k, v in r.details.items():
                    print(f"         {k}: {v}")

        passed = sum(1 for r in results if r.success)
        print(f"\n  {passed}/{len(results)} scenarios passed")

    return 0


def _chaos_status(args: argparse.Namespace) -> int:
    from axiom.vega.federation.chaos import ChaosRunner

    runner = ChaosRunner()
    results = runner.get_results()

    if getattr(args, "json", False):
        print(json.dumps(results, indent=2))
    else:
        if not results:
            print("No chaos test results found.")
            return 0
        print("Previous chaos test results:")
        for r in results:
            status = "PASS" if r.get("success") else "FAIL"
            print(f"  [{status}] {r.get('scenario', '?')}  ({r.get('timestamp', '?')})")
    return 0


# Entry points for extension TOML
def main(argv: list[str] | None = None) -> int:
    """Default entry — security CLI."""
    return security_main(argv)
