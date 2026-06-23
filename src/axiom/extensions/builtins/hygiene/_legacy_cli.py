# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI handler for `axi tidy` — TIDY resource steward commands.

Usage:
    axi hygiene status       Base path, disk free, active entries, pressure level
    axi hygiene ls           Table of all tracked entries
    axi hygiene clean        Sweep expired + orphaned entries
    axi hygiene purge        Delete everything (confirmation prompt)
    axi hygiene stat vitals       Live vitals: disk %, mem %, trend arrows, top owners
    axi hygiene stat health       Node health audit (misconfigurations, freeze detection)
    axi hygiene diagnose     Trigger Layer 3 LLM diagnosis (requires gateway)
    axi hygiene stat retention    Data retention status, dry-run preview, and cleanup
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from axiom.infra.time_utils import time_ago


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi hygiene",
        description="TIDY — Autonomous Resource Steward",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  axi hygiene status       # Show scratch space status
  axi hygiene ls           # List all tracked entries
  axi hygiene clean        # Sweep expired and orphaned entries
  axi hygiene purge        # Delete all scratch entries
  axi hygiene stat vitals       # Detailed vitals snapshot
  axi hygiene diagnose     # LLM-powered diagnosis
""",
    )

    sub = parser.add_subparsers(dest="action")

    sub.add_parser("status", help="Show TIDY status")
    sub.add_parser("ls", help="List all tracked entries")
    clean_p = sub.add_parser("clean", help="Sweep expired and orphaned entries")
    clean_p.add_argument(
        "--repo", action="store_true", help="Also clean repo clutter (pycache, DS_Store, etc.)"
    )
    clean_p.add_argument("--dry-run", action="store_true", help="Show what would be cleaned")

    purge_p = sub.add_parser("purge", help="Delete all scratch entries")
    purge_p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
    )

    sub.add_parser("vitals", help="Detailed vitals snapshot")
    health_p = sub.add_parser(
        "health", help="Node health audit (misconfigurations, freeze detection)"
    )
    health_p.add_argument("--json", action="store_true", help="Output as JSON")
    sub.add_parser("diagnose", help="LLM-powered diagnosis (requires gateway)")

    ci_p = sub.add_parser("ci", help="Check CI/CD pipeline status")
    ci_p.add_argument("--json", action="store_true", help="Output as JSON")

    wt_p = sub.add_parser(
        "worktrees", help="Identify and prune stale git worktrees (with citations)"
    )
    wt_p.add_argument(
        "--repo",
        default=".",
        help="Repository root to scan (default: current directory)",
    )
    wt_p.add_argument(
        "--prune",
        action="store_true",
        help="Remove worktrees that fired any strong staleness signal",
    )
    wt_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be pruned without removing",
    )
    wt_p.add_argument(
        "--force",
        action="store_true",
        help="Allow pruning even when worktree has uncommitted changes (use with care)",
    )
    wt_p.add_argument(
        "--only",
        nargs="+",
        metavar="PATH",
        help="Prune ONLY these worktree paths (also reclaims a locked one — explicit intent)",
    )
    wt_p.add_argument(
        "--exclude",
        nargs="+",
        metavar="PATH",
        help="Never prune these worktree paths",
    )

    br_p = sub.add_parser(
        "branches",
        help="Prune merged local/remote branches (archived first; reversible)",
    )
    br_p.add_argument(
        "--repo", default=".",
        help="Repository root (default: current directory)",
    )
    br_p.add_argument(
        "--prune", action="store_true",
        help="Delete the merged branches (default: list only)",
    )
    br_p.add_argument(
        "--remote", action="store_true",
        help="Operate on merged REMOTE refs instead of local branches",
    )
    br_p.add_argument(
        "--remote-name", default="origin",
        help="Remote name when --remote (default: origin)",
    )
    br_p.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be pruned without deleting",
    )
    br_p.add_argument(
        "--yes", action="store_true",
        help="Confirm an over-limit batch (clears the volume safety prompt)",
    )

    drift_p = sub.add_parser(
        "drift",
        help="Per-worktree drift dashboard + HITL decision packets (read-only)",
    )
    drift_p.add_argument(
        "--repo",
        default=".",
        help="Repository root to scan (default: current directory)",
    )
    drift_p.add_argument(
        "--workspace",
        default=None,
        help="Walk every git repo under this dir (overrides --repo when set)",
    )
    drift_p.add_argument(
        "--branch",
        default=None,
        help="Show the full decision packet for one branch (otherwise: dashboard)",
    )

    ret_p = sub.add_parser("retention", help="Data retention status and cleanup")
    ret_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be cleaned up without deleting",
    )
    ret_p.add_argument(
        "--cleanup",
        action="store_true",
        help="Execute retention cleanup (delete expired files)",
    )

    sub.add_parser(
        "discover",
        help="List git repos under AXI_WORKSPACE_ROOT (respects exclude_repos.json)",
    )

    propose_p = sub.add_parser(
        "propose",
        help="Ask the RACI ledger whether to run an automation class",
    )
    propose_p.add_argument("action_class", help="Stable string identifying the automation")

    approve_p = sub.add_parser(
        "approve",
        help="Record approval for an automation class (future proposals → AUTO)",
    )
    approve_p.add_argument("action_class", help="Stable string identifying the automation")

    deny_p = sub.add_parser(
        "deny",
        help="Record denial for an automation class (future proposals → SKIP)",
    )
    deny_p.add_argument("action_class", help="Stable string identifying the automation")

    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.action:
        args.action = "status"

    handlers = {
        "status": _cmd_status,
        "ls": _cmd_ls,
        "clean": _cmd_clean,
        "purge": _cmd_purge,
        "vitals": _cmd_vitals,
        "health": _cmd_health,
        "diagnose": _cmd_diagnose,
        "ci": _cmd_ci,
        "retention": _cmd_retention,
        "worktrees": _cmd_worktrees,
        "branches": _cmd_branches,
        "drift": _cmd_drift,
        "discover": _cmd_discover,
        "propose": _cmd_propose,
        "approve": _cmd_approve,
        "deny": _cmd_deny,
    }

    handler = handlers.get(args.action)
    if handler is None:
        parser.print_help()
        return 1

    return handler(args)


def _get_manager():
    from . import manager

    return manager()


def _cmd_status(args) -> int:
    mgr = _get_manager()
    info = mgr.status()

    if getattr(args, "json", False):
        print(json.dumps(info, indent=2))
        return 0

    print("TIDY Status")
    print(f"  Base:      {info['base_dir']}")

    used = _fmt_bytes(info["total_size_bytes"])
    free = _fmt_bytes(info["disk_free_bytes"])
    pct = info["disk_used_pct"]
    print(f"  Disk:      {used} used  {free} free ({pct}%)")

    # Memory (if psutil available)
    try:
        import os

        import psutil

        rss = psutil.Process(os.getpid()).memory_info().rss
        print(f"  Memory:    {_fmt_bytes(rss)} RSS")
    except (ImportError, Exception):
        pass

    # Pressure
    try:
        from .network import NetworkLedger
        from .vitals import VitalsMonitor

        monitor = VitalsMonitor(mgr, NetworkLedger.shared())
        monitor.sample()
        pressure = monitor.check_pressure()
        print(f"  Pressure:  {pressure}")

        leaks = monitor.detect_leaks()
        if leaks:
            print(f"  Leaks:     {len(leaks)} detected")
            for leak in leaks:
                print(f"             {leak.evidence}")
        else:
            print("  Leaks:     none detected")
    except Exception:
        print("  Pressure:  unknown (vitals unavailable)")

    # Entry summary
    entries = mgr.all_entries()
    dirs = sum(1 for e in entries if e.is_dir)
    files = len(entries) - dirs
    parts = []
    if dirs:
        parts.append(f"{dirs} dir{'s' if dirs != 1 else ''}")
    if files:
        parts.append(f"{files} file{'s' if files != 1 else ''}")
    detail = f" ({', '.join(parts)})" if parts else ""
    print(f"  Active:    {len(entries)} entries{detail}")

    if not entries:
        print()
        return 0

    # Entry table
    print()
    print(f"  {'Owner':<20} {'Type':<6} {'Retention':<10} {'Age':<9} {'Size':<10}")
    now = datetime.now(UTC)
    for e in entries:
        etype = "dir" if e.is_dir else "file"
        age = time_ago(e.created_at, now=now)
        from pathlib import Path

        size = _fmt_bytes(mgr._measure_size(Path(e.path), e.is_dir))
        print(f"  {e.owner:<20} {etype:<6} {e.retention:<10} {age:<9} {size:<10}")

    print()
    return 0


def _cmd_ls(args) -> int:
    mgr = _get_manager()
    entries = mgr.all_entries()

    if getattr(args, "json", False):
        print(json.dumps([e.to_dict() for e in entries], indent=2))
        return 0

    if not entries:
        print("No active TIDY entries.")
        return 0

    now = datetime.now(UTC)
    print(f"{'ID':<14} {'Owner':<20} {'Type':<6} {'Retention':<10} {'PID':<8} {'Age':<9} {'Path'}")
    print("-" * 100)
    for e in entries:
        etype = "dir" if e.is_dir else "file"
        age = time_ago(e.created_at, now=now)
        # Shorten path for display
        path = e.path
        if len(path) > 40:
            path = "..." + path[-37:]
        print(f"{e.id:<14} {e.owner:<20} {etype:<6} {e.retention:<10} {e.pid:<8} {age:<9} {path}")

    print(f"\n{len(entries)} entries")
    return 0


def _cmd_clean(args) -> int:
    mgr = _get_manager()
    result = mgr.sweep()

    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
        return 0

    total = result["expired"] + result["orphaned"]
    if total == 0:
        print("Nothing to clean (scratch).")
    else:
        print(
            f"Cleaned {total} entries ({result['expired']} expired, {result['orphaned']} orphaned)"
        )
        if result["errors"]:
            print(f"  {result['errors']} errors during cleanup")

    # Repo hygiene (--repo flag)
    if getattr(args, "repo", False):
        from axiom import REPO_ROOT

        from .repo_hygiene import clean_clutter, scan_repo_hygiene

        dry_run = getattr(args, "dry_run", False)
        print()
        findings = scan_repo_hygiene(REPO_ROOT)

        if findings["clutter"]:
            print(f"Repo clutter ({len(findings['clutter'])} items):")
            for path, _item_type, desc in findings["clutter"][:20]:
                print(f"  {path} ({desc})")
            if not dry_run:
                cleaned = clean_clutter(REPO_ROOT, dry_run=False)
                print(f"  Cleaned: {cleaned['dirs']} dirs, {cleaned['files']} files")
        else:
            print("Repo is clean.")

        if findings.get("stale_neut"):
            print(f"\nStale .neut/ items: {', '.join(findings['stale_neut'])}")
            if not dry_run:
                import shutil

                from axiom import REPO_ROOT

                for name in findings["stale_neut"]:
                    stale_path = REPO_ROOT / ".neut" / name
                    if stale_path.is_dir():
                        shutil.rmtree(stale_path, ignore_errors=True)
                    elif stale_path.is_file():
                        stale_path.unlink(missing_ok=True)
                    print(f"  Removed .neut/{name}")

        if findings["unexpected_root"]:
            print(f"\nUnexpected root items: {', '.join(findings['unexpected_root'])}")
            print("  New functionality → src/axiom/extensions/builtins/")
            print("  One-off scripts → spikes/")

    return 0


def _cmd_purge(args) -> int:
    mgr = _get_manager()
    entries = mgr.all_entries()

    if not entries:
        print("Nothing to purge.")
        return 0

    if not getattr(args, "yes", False):
        print(f"This will delete {len(entries)} entries and all scratch data.")
        try:
            response = input("Continue? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1
        if response.lower() not in ("y", "yes"):
            print("Aborted.")
            return 1

    result = mgr.purge()

    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
        return 0

    print(f"Purged {result['deleted']} entries.")
    return 0


def _cmd_vitals(args) -> int:
    try:
        from .network import NetworkLedger
        from .vitals import VitalsMonitor
    except ImportError as e:
        print(f"Vitals unavailable: {e}")
        return 1

    mgr = _get_manager()
    monitor = VitalsMonitor(mgr, NetworkLedger.shared())
    snap = monitor.sample()
    pressure = monitor.check_pressure()
    leaks = monitor.detect_leaks()

    if getattr(args, "json", False):
        data = snap.to_dict()
        data["pressure"] = pressure
        data["leaks"] = [
            {"owner": leak.owner, "pattern": leak.pattern, "evidence": leak.evidence}
            for leak in leaks
        ]
        print(json.dumps(data, indent=2))
        return 0

    print("TIDY Vitals")
    print("=" * 50)
    print(
        f"  Scratch:   {_fmt_bytes(snap.scratch_used_bytes)} used / "
        f"{_fmt_bytes(snap.scratch_free_bytes)} free "
        f"({snap.scratch_pct:.1f}%)"
    )

    if snap.process_rss_bytes is not None:
        print(f"  Memory:    {_fmt_bytes(snap.process_rss_bytes)} RSS")
    if snap.system_mem_pct is not None:
        print(f"  System:    {snap.system_mem_pct:.1f}% memory used")

    print(f"  Pressure:  {pressure}")
    print(f"  Entries:   {snap.active_entries}")

    if snap.entries_by_owner:
        print()
        print("  Top owners:")
        for owner, count in sorted(
            snap.entries_by_owner.items(),
            key=lambda x: snap.bytes_by_owner.get(x[0], 0),
            reverse=True,
        )[:5]:
            size = _fmt_bytes(snap.bytes_by_owner.get(owner, 0))
            print(f"    {owner:<25} {count} entries  {size}")

    if snap.net and snap.net.total_requests > 0:
        print()
        print("  Network (5m window):")
        print(
            f"    Requests:  {snap.net.total_requests} "
            f"({snap.net.total_errors} errors, {snap.net.error_rate_pct:.1f}%)"
        )
        print(
            f"    Latency:   avg {snap.net.avg_latency_ms:.0f}ms, "
            f"p95 {snap.net.p95_latency_ms:.0f}ms"
        )
        if snap.net.anomalies:
            print(f"    Anomalies: {len(snap.net.anomalies)}")
            for a in snap.net.anomalies:
                print(f"      [{a.severity}] {a.kind}: {a.evidence}")

    if leaks:
        print()
        print(f"  Leaks ({len(leaks)}):")
        for leak in leaks:
            print(f"    [{leak.pattern}] {leak.evidence}")

    print()
    return 0


def _cmd_health(args) -> int:
    from .node_health import Severity, audit_node

    report = audit_node()

    if getattr(args, "json", False):
        print(json.dumps(report.to_dict(), indent=2))
        return 0

    print("TIDY Node Health Audit")
    print("=" * 50)

    if report.healthy:
        print("  All checks passed — node is healthy.")
    else:
        severity_icons = {
            Severity.CRITICAL: "\033[31mCRIT\033[0m",
            Severity.WARNING: "\033[33mWARN\033[0m",
            Severity.INFO: "\033[36mINFO\033[0m",
        }

        for f in report.findings:
            icon = severity_icons.get(f.severity, f.severity.value)
            fix = " (auto-fixable)" if f.auto_fixable else ""
            print(f"  [{icon}] {f.check}: {f.message}{fix}")
            if f.current_value:
                print(f"         current: {f.current_value}  expected: {f.expected_value}")

    if report.journal_gaps:
        print()
        print(f"  Journal gaps ({len(report.journal_gaps)} detected):")
        for gap in report.journal_gaps:
            hours = gap.gap.total_seconds() / 3600
            if hours >= 24:
                duration = f"{hours / 24:.1f} days"
            else:
                duration = f"{hours:.1f} hours"
            print(
                f"    {gap.last_entry:%Y-%m-%d %H:%M} → {gap.next_boot:%Y-%m-%d %H:%M} ({duration} gap)"
            )

    print()
    summary = []
    if report.critical_count:
        summary.append(f"{report.critical_count} critical")
    if report.warning_count:
        summary.append(f"{report.warning_count} warnings")
    info_count = sum(1 for f in report.findings if f.severity == Severity.INFO)
    if info_count:
        summary.append(f"{info_count} info")
    if summary:
        print(f"  Summary: {', '.join(summary)}")
    else:
        print("  Summary: clean")

    return 0 if report.critical_count == 0 else 1


def _cmd_ci(args) -> int:
    """Check CI/CD pipeline status across all configured remotes."""
    import json as json_mod

    # Find the repo root
    import subprocess
    from pathlib import Path

    from axiom.infra.git import safe_git_env

    from .ci_watcher import get_ci_summary

    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
        env=safe_git_env(),
    )
    repo_dir = Path(result.stdout.strip()) if result.returncode == 0 else Path.cwd()

    # Check prerequisites and guide user through setup
    if not (repo_dir / ".git").exists() and result.returncode != 0:
        print("Not a git repository.")
        print("  Run `git init` to initialize, or `cd` to a project directory.")
        return 1

    from .ci_watcher import discover_ci_providers

    providers_raw = discover_ci_providers(repo_dir)

    if not providers_raw:
        # Check if there are remotes at all
        rc2, _remotes = (
            subprocess.run(
                ["git", "remote", "-v"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                cwd=str(repo_dir),
                env=safe_git_env(repo_dir),
            ).stdout,
            "",
        )
        if not rc2.strip() if isinstance(rc2, str) else True:
            print("No git remotes configured.")
            print("  Add a remote to enable CI monitoring:")
            print("    git remote add origin https://github.com/your-org/your-repo.git")
            print("    git remote add origin https://your-gitlab.com/your-org/your-repo.git")
        else:
            print("Git remotes found but no supported CI providers detected.")
            print("  Supported: GitHub, GitLab, Gitea, Bitbucket")
        return 1

    summary = get_ci_summary(repo_dir)

    if getattr(args, "json", False):
        print(json_mod.dumps(summary, indent=2))
        return 0

    # Show discovered providers
    providers = summary.get("providers", [])
    if providers:
        print("CI Providers (auto-discovered from git remotes):")
        for p in providers:
            auth = "\033[32mauth\033[0m" if p["authenticated"] else "\033[31mno auth\033[0m"
            mirror = " \033[90m(mirror)\033[0m" if p.get("is_mirror") else ""
            print(f"  {p['name']}: {p['type']} ({p['project']}) [{auth}]{mirror}")

        # Guide for unauthenticated providers
        unauthed = [p for p in providers if not p["authenticated"]]
        if unauthed:
            print()
            for p in unauthed:
                if p["type"] == "gitlab":
                    print(f"  To authenticate {p['name']}: export GITLAB_TOKEN=<your-token>")
                    print("    or: axi connect gitlab")
                elif p["type"] == "github":
                    print(f"  To authenticate {p['name']}: gh auth login")
                    print("    or: export GITHUB_TOKEN=<your-token>")
        print()

    if not providers:
        print("No CI providers found. Add a GitHub or GitLab remote.")
        return 0

    if summary["healthy"] and summary["in_progress_count"] == 0:
        print("CI Status: all green")
        return 0

    if summary["in_progress_count"] > 0:
        print(f"CI in progress ({summary['in_progress_count']}):")
        for run in summary["in_progress_runs"]:
            print(f"  [{run['provider']}/{run['remote']}] {run['branch']} — {run['url']}")

    if summary["failed_count"] > 0:
        print(f"\nCI FAILED ({summary['failed_count']}):")
        for run in summary["failed_runs"]:
            jobs = ", ".join(run["failed_jobs"]) if run["failed_jobs"] else "check logs"
            print(f"  [{run['provider']}/{run['remote']}] {run['branch']} — {jobs}")
            print(f"    {run['url']}")
        return 1

    return 0


def _cmd_diagnose(args) -> int:
    print("TIDY Diagnosis (LLM-powered)")
    print("=" * 50)

    try:
        from axiom.infra.gateway import Gateway

        gateway = Gateway()
        if not gateway.available:
            print("No LLM gateway available. Configure ANTHROPIC_API_KEY or OPENAI_API_KEY.")
            return 1
    except ImportError:
        print("Gateway module not found.")
        return 1

    mgr = _get_manager()

    try:
        from .agent import MoAgent
        from .network import NetworkLedger
        from .vitals import VitalsMonitor

        monitor = VitalsMonitor(mgr, NetworkLedger.shared())
        snap = monitor.sample()
        pressure = monitor.check_pressure()
        leaks = monitor.detect_leaks()

        agent = MoAgent(gateway=gateway)
        agent.set_manager(mgr, monitor)

        signal = {
            "type": "manual_diagnosis",
            "level": pressure,
            "vitals": snap.to_dict(),
            "leaks": [
                {"owner": leak.owner, "pattern": leak.pattern, "evidence": leak.evidence}
                for leak in leaks
            ],
        }

        print("Analyzing...")
        verdict = agent.diagnose(signal)

        print(f"\nLevel: {verdict.level}")
        print(f"\nDiagnosis:\n{verdict.diagnosis}")

        if verdict.actions_taken:
            print("\nActions taken:")
            for action in verdict.actions_taken:
                print(f"  - {action}")

        if verdict.recommendations:
            print("\nRecommendations:")
            for rec in verdict.recommendations:
                print(f"  - {rec}")

    except Exception as e:
        print(f"Diagnosis failed: {e}")
        return 1

    print()
    return 0


def _cmd_retention(args) -> int:
    from axiom import REPO_ROOT

    try:
        from .retention import (
            execute_retention,
            load_retention_config,
            retention_status,
            scan_retention,
        )
    except ImportError as e:
        print(f"Retention unavailable: {e}")
        return 1

    config_dir = REPO_ROOT / "runtime" / "config"
    example_dir = REPO_ROOT / "runtime" / "config.example"
    policies, legal_hold, audit_path = load_retention_config(config_dir, example_dir)

    if not policies:
        print("No retention policies configured.")
        print(f"  Copy {example_dir / 'retention.yaml'} to {config_dir / 'retention.yaml'}")
        return 0

    if getattr(args, "json", False):
        status = retention_status(REPO_ROOT, policies, legal_hold)
        # Convert Path objects for JSON serialization
        for cat in status.get("categories", []):
            cat["actions"] = [
                {"path": str(a.path), "age_days": a.age_days, "size_bytes": a.size_bytes}
                for a in cat.get("actions", [])
            ]
        print(json.dumps(status, indent=2, default=str))
        return 0

    dry_run = getattr(args, "dry_run", False)
    cleanup = getattr(args, "cleanup", False)

    if cleanup or dry_run:
        actions = scan_retention(REPO_ROOT, policies, legal_hold)
        if not actions:
            print("Nothing to clean up — all data within retention policies.")
            return 0

        label = "Retention Cleanup Preview (dry run)" if dry_run else "Retention Cleanup"
        print(label)
        print("=" * len(label))

        for a in actions:
            action_str = a.action if not dry_run else "would delete"
            if a.action == "skip":
                action_str = "SKIP (legal hold)"
            print(f"  {action_str}: {a.path}")
            print(
                f"           {a.age_days}d old, {_fmt_bytes(a.size_bytes)}, policy={a.policy_key}"
            )

        total_bytes = sum(a.size_bytes for a in actions if a.action == "delete")
        print(f"\n  Total: {len(actions)} files, {_fmt_bytes(total_bytes)} recoverable")

        if not dry_run:
            result = execute_retention(actions, REPO_ROOT / audit_path)
            print(
                f"\n  Deleted: {result['deleted']}  Skipped: {result['skipped']}  "
                f"Freed: {_fmt_bytes(result['bytes_freed'])}"
            )
            if result["errors"]:
                print(f"  Errors: {result['errors']}")
            print(f"  Audit log: {REPO_ROOT / audit_path}")
        return 0

    # Default: show status
    status = retention_status(REPO_ROOT, policies, legal_hold)

    print("Retention Policy Status")
    print("=" * 50)

    if legal_hold:
        print("  ⚠  LEGAL HOLD ACTIVE — no automated deletion")
        print()

    if not status["categories"]:
        print("  All data within retention policies.")
    else:
        for cat in status["categories"]:
            print(f"  {cat['policy_key']} ({cat['days']}d after {cat['after']})")
            print(
                f"    {cat['files']} files past retention ({_fmt_bytes(cat['bytes'])} recoverable)"
            )

        print()
        print(
            f"  Total: {status['total_files']} files, {_fmt_bytes(status['total_bytes'])} recoverable"
        )
        print()
        print("  Run `axi hygiene stat retention --dry-run` to preview cleanup")
        print("  Run `axi hygiene stat retention --cleanup` to execute")

    print()
    return 0


def _cmd_drift(args) -> int:
    """`axi hygiene stat drift` — surface drifting branches with HITL decision packets."""
    from pathlib import Path

    from . import drift as drift_mod

    if args.workspace:
        ws = Path(args.workspace).expanduser().resolve()
        # Every entry with a .git (file or dir) is a candidate repo or worktree.
        # Multiple worktrees of the same repo share a git-common-dir; keep only
        # one entry per common-dir so we don't list the same logical repo twice.
        candidates = sorted(p.parent for p in ws.glob("*/.git"))
        seen_common_dirs: set[Path] = set()
        repos: list[Path] = []
        for cand in candidates:
            rc, common = drift_mod._run(
                ["git", "rev-parse", "--git-common-dir"], cwd=cand
            )
            if rc != 0:
                continue
            common_path = (cand / common.strip()).resolve()
            if common_path in seen_common_dirs:
                continue
            seen_common_dirs.add(common_path)
            repos.append(cand)
        per_repo = drift_mod.gather_drift_across_repos(repos)
    else:
        repo = Path(args.repo).expanduser().resolve()
        per_repo = {repo: drift_mod.gather_drift(repo)}

    if args.branch:
        # Find the matching branch across all scanned repos and print its packet.
        for reports in per_repo.values():
            for r in reports:
                if r.branch == args.branch:
                    print(r.decision_packet)
                    return 0
        print(f"No drifting worktree found with branch '{args.branch}'.", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        payload = {
            str(repo): [
                {
                    "branch": r.branch,
                    "path": str(r.path),
                    "ahead": r.ahead,
                    "behind": r.behind,
                    "unpushed": r.unpushed,
                    "dirty_files": r.dirty_files,
                    "last_commit_age_days": r.last_commit_age_days,
                    "has_open_pr": r.has_open_pr,
                    "pr_state": r.pr_state,
                    "drift_severity": r.drift_severity,
                    "suggested_action": r.suggested_action,
                    "purpose": {
                        "topic": r.purpose.inferred_topic,
                        "related_adrs": r.purpose.related_adrs,
                        "related_prds": r.purpose.related_prds,
                    },
                }
                for r in reports
            ]
            for repo, reports in per_repo.items()
        }
        print(json.dumps(payload, indent=2))
        return 0

    total = sum(len(rs) for rs in per_repo.values())
    if total == 0:
        print("No drifting worktrees found.")
        return 0

    for repo, reports in per_repo.items():
        if not reports:
            continue
        print(f"\n=== {repo}  ({len(reports)} worktrees) ===\n")
        print(drift_mod.render_dashboard(reports))

    print(
        "\nRun `axi hygiene stat drift --branch <name>` for the full decision packet "
        "(purpose + recent commits + suggested action rationale).\n"
    )
    return 0


def _cmd_worktrees(args) -> int:
    from pathlib import Path

    from . import worktrees as wt_mod

    repo = Path(args.repo).resolve()
    verdicts = wt_mod.find_stale(repo)
    stale = [v for v in verdicts if v.is_stale]

    if getattr(args, "json", False):
        payload = [
            {
                "path": str(v.worktree.path),
                "branch": v.worktree.branch,
                "head": v.worktree.head_sha,
                "is_stale": v.is_stale,
                "is_dirty": v.is_dirty,
                "can_force_prune": v.can_force_prune,
                "reasons": v.reasons,
            }
            for v in verdicts
        ]
        print(json.dumps(payload, indent=2))
        return 0

    if not verdicts:
        print(f"No worktrees found for {repo}.")
        return 0

    print(f"Worktrees for {repo} ({len(verdicts)} total, {len(stale)} stale):\n")
    for v in verdicts:
        wt = v.worktree
        marker = "STALE" if v.is_stale else "ok   "
        dirty = " (dirty)" if v.is_dirty else ""
        branch = wt.branch or "(detached)"
        print(f"  [{marker}] {wt.path}{dirty}")
        print(f"           branch: {branch}  head: {wt.head_sha[:10]}")
        for reason in v.reasons:
            print(f"           - {reason}")

    if not stale:
        print("\nNothing to prune.")
        return 0

    if not args.prune and not args.dry_run:
        print("\nRun `axi hygiene list worktrees --prune` to remove stale worktrees,")
        print("or `axi hygiene list worktrees --dry-run` to preview the exact commands.")
        return 0

    print()
    import subprocess

    from axiom.infra.git import safe_git_env

    # Deterministic safety floors (never auto-remove locked/dirty/unselected
    # work); the staleness signals above are TIDY's judgment layer.
    plan = wt_mod.plan_prune(
        verdicts,
        force=args.force,
        only=getattr(args, "only", None),
        exclude=getattr(args, "exclude", None),
    )
    for v, reason in plan.skipped:
        print(f"  [skip ] {v.worktree.path}  — {reason}")

    pruned = 0
    errors = 0
    for v in plan.to_prune:
        wt = v.worktree
        cmd = ["git", "worktree", "remove", str(wt.path)]
        if v.is_dirty or v.can_force_prune is False:
            cmd.append("--force")
        if args.dry_run:
            if wt.locked:
                print(f"  [plan ] git worktree unlock {wt.path}")
            print("  [plan ]", " ".join(cmd))
            continue
        # A locked worktree only reaches to_prune via an explicit --only, which
        # is the operator taking responsibility — unlock it, then remove.
        if wt.locked:
            subprocess.run(
                ["git", "worktree", "unlock", str(wt.path)],
                check=False,
                env=safe_git_env(),
            )
        rc = subprocess.run(cmd, check=False, env=safe_git_env()).returncode
        if rc == 0:
            print(f"  [pruned] {wt.path}")
            pruned += 1
        else:
            print(f"  [error] git worktree remove failed for {wt.path} (rc={rc})")
            errors += 1

    if args.dry_run:
        print(f"\n{len(plan.to_prune)} would be pruned, {len(plan.skipped)} skipped.")
    else:
        tail = f", {errors} errors." if errors else "."
        print(f"\nPruned {pruned}, skipped {len(plan.skipped)}{tail}")
    return 0


def _cmd_branches(args) -> int:
    """List or prune merged branches (local default, remote with --remote).

    TIDY owns destructive git cleanup (ADR-046); each delete archives the
    ref under refs/tidy-archive/ first (reversible per ADR-045 D6.2), and
    an over-limit batch downgrades to a confirmation prompt (D6.3).
    """
    import os
    from pathlib import Path

    from . import branch_prune as bp

    repo = Path(args.repo).resolve()
    remote = bool(getattr(args, "remote", False))
    remote_name = getattr(args, "remote_name", "origin")
    scope = f"remote ({remote_name})" if remote else "local"

    pairs = (
        bp.list_merged_remote(repo, remote_name) if remote
        else bp.list_merged_local(repo)
    )

    if getattr(args, "json", False) and not args.prune:
        print(json.dumps(
            [{"branch": b, "sha": s, "scope": "remote" if remote else "local"}
             for b, s in pairs],
            indent=2,
        ))
        return 0

    if not pairs:
        print(f"No merged {scope} branches to prune in {repo}.")
        return 0

    print(f"Merged {scope} branches in {repo} ({len(pairs)}):")
    for b, s in pairs:
        print(f"  {b}  ({s[:10]})")

    if not args.prune and not args.dry_run:
        flag = " --remote" if remote else ""
        print(f"\nRun `axi hygiene list branches --prune{flag}` to delete these")
        print("(each archived under refs/tidy-archive/ first — reversible).")
        return 0

    state_dir = Path(os.environ.get("AXI_STATE_DIR") or (Path.home() / ".axi"))
    result = bp.prune(
        repo, state_dir=state_dir, remote=remote, remote_name=remote_name,
        dry_run=bool(args.dry_run), confirmed=bool(getattr(args, "yes", False)),
    )

    if result.reason == "dry_run":
        print("\nWould prune (dry-run):")
        for b in result.would_prune:
            print(f"  {b}")
        return 0
    if not result.proceed and result.reason.startswith("needs_confirmation"):
        n = len(result.would_prune)
        print(f"\nBatch of {n} exceeds the per-tick safety limit "
              f"({result.reason}).")
        print(f"Re-run with --yes to confirm pruning all {n}.")
        return 2
    if not result.proceed:
        print(f"\nRefused: {result.reason}")
        return 1
    print(f"\nPruned {len(result.pruned)}; {len(result.failed)} failed.")
    if result.pruned:
        print("Archived under refs/tidy-archive/ — recoverable.")
    return 0 if not result.failed else 1


# --- RACI ledger commands ---


def _raci_state_dir():
    """TIDY's RACI state lives under $AXI_STATE_DIR / agents / tidy / .

    When AXI_STATE_DIR is unset, falls back to ~/.axi/agents/tidy.
    """
    import os
    from pathlib import Path

    base = os.environ.get("AXI_STATE_DIR")
    if base:
        root = Path(base)
    else:
        root = Path.home() / ".axi"
    return root / "agents" / "tidy"


def _raci_ledger_path():
    return _raci_state_dir() / "raci_state.json"


def _load_raci_ledger():
    from axiom.agents.raci import RACILedger

    return RACILedger.load(_raci_ledger_path())


def _save_raci_ledger(ledger) -> None:
    ledger.save(_raci_ledger_path())


def _cmd_discover(args) -> int:
    """List git repositories under AXI_WORKSPACE_ROOT.

    Honors `exclude_repos.json` (a list of repo basenames) when present
    in the TIDY state dir.
    """
    import json as _json
    import os
    from pathlib import Path

    workspace = os.environ.get("AXI_WORKSPACE_ROOT")
    if not workspace:
        workspace = str(Path.cwd())
    ws = Path(workspace)

    repos: list[Path] = []
    if ws.exists():
        for entry in sorted(ws.iterdir()):
            if entry.is_dir() and (entry / ".git").exists():
                repos.append(entry)

    exclude_path = _raci_state_dir() / "exclude_repos.json"
    excluded: set[str] = set()
    if exclude_path.exists():
        try:
            excluded = set(_json.loads(exclude_path.read_text()))
        except (OSError, ValueError):
            excluded = set()

    visible = [r for r in repos if r.name not in excluded]
    excluded_count = len(repos) - len(visible)

    print(f"Discovered repos under {ws}:")
    for r in visible:
        print(f"  {r.name} @ {r}")
    print()
    print(f"{len(visible)} repo(s) discovered.")
    if excluded_count > 0:
        print(f"{excluded_count} excluded by config ({exclude_path}).")
    return 0


def _cmd_propose(args) -> int:
    """Print the RACI decision for an automation class."""
    ledger = _load_raci_ledger()
    decision = ledger.propose(args.action_class)
    _save_raci_ledger(ledger)
    print(f"{args.action_class}: {decision.value.upper()}")
    return 0


def _cmd_approve(args) -> int:
    """Record approval — future proposals for this class return AUTO."""
    ledger = _load_raci_ledger()
    ledger.record_yes(args.action_class)
    _save_raci_ledger(ledger)
    print(f"{args.action_class}: approved.")
    return 0


def _cmd_deny(args) -> int:
    """Record denial — future proposals for this class return SKIP."""
    ledger = _load_raci_ledger()
    ledger.record_no(args.action_class)
    _save_raci_ledger(ledger)
    print(f"{args.action_class}: denied.")
    return 0


# --- Helpers ---


def _fmt_bytes(n: int) -> str:
    if n < 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n = int(n / 1024)
    return f"{n:.1f} TB"


if __name__ == "__main__":
    sys.exit(main())
