# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI for TRIAGE — diagnostics agent.

Bound to the `axi triage` noun (see this extension's manifest). The
heartbeat subcommand is what launchd / systemd fires on a configurable
interval (default 600s) when TRIAGE is registered as a daemon agent.
"""

from __future__ import annotations

import argparse
import json

from axiom.infra.paths import get_user_state_dir

from . import safety


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi triage",
        description="TRIAGE — diagnostics + integrity agent",
    )
    sub = parser.add_subparsers(dest="action")

    sub.add_parser(
        "heartbeat",
        help="Proactive sweep — run all registered safety checks, log findings",
    )
    sub.add_parser(
        "checks",
        help="List registered safety checks (built-ins + extension-contributed)",
    )
    sweep_p = sub.add_parser("sweep", help="Run a sweep on demand and print results")
    sweep_p.add_argument("--format", choices=["human", "json"], default="human")

    pending_p = sub.add_parser(
        "pending",
        help="List pending CLI-failure diagnoses (matched by TRIAGE on cli.arg_error)",
    )
    pending_p.add_argument("--format", choices=["human", "json"], default="human")

    clear_p = sub.add_parser(
        "clear",
        help="Acknowledge / dismiss a pending diagnosis (resolved or not relevant)",
    )
    clear_p.add_argument(
        "fingerprint",
        nargs="?",
        help="Diagnosis fingerprint to clear (omit + use --all to clear everything)",
    )
    clear_p.add_argument(
        "--all",
        action="store_true",
        dest="clear_all",
        help="Clear ALL pending diagnoses",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.action:
        parser.print_help()
        return 1

    handlers = {
        "heartbeat": _cmd_heartbeat,
        "checks": _cmd_checks,
        "sweep": _cmd_sweep,
        "pending": _cmd_pending,
        "clear": _cmd_clear,
    }
    return handlers[args.action](args)


def _cmd_heartbeat(args: argparse.Namespace) -> int:
    """Proactive sweep — what launchd/systemd fires on the daemon timer.

    Runs all registered safety checks (TRIAGE's built-ins + any extension-
    contributed checks via the [[extension.provides]] kind="safety_check"
    pattern). Persists a structured sweep entry to
    ~/.axi/agents/triage/sweep.jsonl.

    Exit code: 0 if no critical findings, 2 if any. Non-zero exits make
    launchd / `axi agents logs` light up loudly.
    """
    result = safety.sweep()

    log_dir = get_user_state_dir() / "agents" / "triage"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "sweep.jsonl"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result) + "\n")

    if result["findings_by_severity"].get(safety.SEVERITY_CRITICAL, 0) > 0:
        return 2
    return 0


def _cmd_checks(args: argparse.Namespace) -> int:
    """List registered safety checks (transparency for operators)."""
    checks = safety.discover_safety_checks()
    if not checks:
        print("No safety checks registered.")
        return 0
    print(f"Registered safety checks ({len(checks)}):\n")
    for name, entry, severity_default in checks:
        print(f"  {name}")
        print(f"    entry:    {entry}")
        print(f"    severity: {severity_default}")
        print()
    return 0


def _cmd_sweep(args: argparse.Namespace) -> int:
    """Run a sweep on demand and print results — does not write to JSONL."""
    result = safety.sweep()

    if getattr(args, "format", "human") == "json":
        print(json.dumps(result, indent=2))
        return 0 if result["findings_by_severity"].get(safety.SEVERITY_CRITICAL, 0) == 0 else 2

    counts = result["findings_by_severity"]
    total = result["findings_total"]
    checks_run = result["checks_run"]

    print(f"TRIAGE Sweep — {checks_run} checks run, {total} finding(s)")
    print("=" * 60)
    print(
        f"  critical: {counts.get(safety.SEVERITY_CRITICAL, 0)}  "
        f"warning: {counts.get(safety.SEVERITY_WARNING, 0)}  "
        f"info: {counts.get(safety.SEVERITY_INFO, 0)}"
    )
    print()

    for f in result["findings"]:
        sev = f["severity"].upper()
        print(f"  [{sev:8s}] {f['check_name']}: {f['title']}")
        if f.get("detail"):
            print(f"            {f['detail']}")
        if f.get("remediation"):
            print(f"            -> {f['remediation']}")
        print()

    if not result["findings"]:
        print("  All clear.")

    return 0 if counts.get(safety.SEVERITY_CRITICAL, 0) == 0 else 2


def _cmd_pending(args: argparse.Namespace) -> int:
    """List pending CLI-failure diagnoses (the per-failure log surfaced
    on the next CLI invocation).  Distinct from the sweep log."""
    from . import cli_listener

    pending = cli_listener.read_pending()

    if getattr(args, "format", "human") == "json":
        print(json.dumps({"pending": pending, "count": len(pending)}, indent=2))
        return 0

    if not pending:
        print("No pending CLI-failure diagnoses.")
        return 0

    print(f"TRIAGE pending diagnoses ({len(pending)}):\n")
    for d in pending:
        print(f"  [{d.get('fingerprint', '?')}] {d.get('summary', '')}")
        print(f"    confidence: {d.get('confidence', 0):.2f}")
        print(f"    matched at: {d.get('matched_at', '?')}")
        if d.get("remedy"):
            print(f"    remedy:     {d['remedy']}")
        print()
    print("Acknowledge with: axi triage clear <fingerprint>")
    print("Or all at once:   axi triage clear --all")
    return 0


def _cmd_clear(args: argparse.Namespace) -> int:
    """Clear one pending diagnosis (by fingerprint) or all of them."""
    from . import cli_listener

    if args.clear_all:
        cli_listener.clear_pending(state_dir=None, fingerprint=None)
        print("Cleared all pending diagnoses.")
        return 0
    if not args.fingerprint:
        print("Provide a fingerprint or use --all.")
        print("List pending with: axi triage pending")
        return 2
    cli_listener.clear_pending(state_dir=None, fingerprint=args.fingerprint)
    print(f"Cleared diagnosis {args.fingerprint}.")
    return 0
