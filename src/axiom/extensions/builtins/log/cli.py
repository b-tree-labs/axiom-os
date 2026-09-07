# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI handler for `axi log` — log inspection, verification, and management.

Usage:
    axi log tail [--follow] [--ec] [--level LEVEL] [--provider PROVIDER] [--n N]
    axi log verify [--table TABLE] [--since DAYS]
    axi log stats
    axi log backend
    axi log sinks
    axi log export [--since DAYS]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from axiom.infra.time_utils import parse_iso

# ---------------------------------------------------------------------------
# JSONL helpers
# ---------------------------------------------------------------------------

def _iter_jsonl(path: Path) -> Iterator[dict]:
    """Yield parsed JSON objects from a JSONL file, skipping malformed lines."""
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _routing_events_path() -> Path | None:
    """Locate the routing_events JSONL file via AuditLog if available."""
    try:
        from axiom.infra.audit_log import AuditLog
        al = AuditLog.get()
        if hasattr(al, "routing_events_path"):
            return Path(al.routing_events_path)  # type: ignore[attr-defined]
    except (ImportError, AttributeError):
        pass
    # Fallback: look in standard runtime location
    try:
        from axiom import REPO_ROOT
        candidate = Path(REPO_ROOT) / "runtime" / "logs" / "routing_events.jsonl"
        if candidate.exists():
            return candidate
    except (ImportError, OSError):
        pass
    return None


def _since_cutoff(days: int | None) -> datetime | None:
    if days is None:
        return None
    return datetime.now(UTC) - timedelta(days=days)


def _parse_ts(record: dict) -> datetime | None:
    raw = record.get("ts") or record.get("timestamp") or record.get("time")
    if not raw:
        return None
    try:
        return parse_iso(str(raw))
    except ValueError:
        return None


def _format_record(record: dict) -> str:
    ts = record.get("ts") or record.get("timestamp") or record.get("time") or "?"
    provider = (
        record.get("llm_provider")
        or record.get("provider")
        or record.get("provider_name")
        or "?"
    )
    tier = record.get("tier") or record.get("route") or record.get("routing_tier") or "?"
    blocked = record.get("blocked", record.get("is_blocked", "?"))
    return f"{ts} | {provider} | {tier} | {blocked}"


# ---------------------------------------------------------------------------
# Level mapping
# ---------------------------------------------------------------------------

_LEVEL_MAP = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "warn": 30,
    "error": 40,
    "critical": 50,
}


def _level_no(name: str) -> int:
    return _LEVEL_MAP.get(name.lower(), 0)


# ---------------------------------------------------------------------------
# Subcommand: tail
# ---------------------------------------------------------------------------

def _cmd_tail(args: argparse.Namespace) -> int:
    path = _routing_events_path()
    if path is None:
        print("axi log: routing_events log file not found", file=sys.stderr)
        return 1

    level_filter = _level_no(args.level) if args.level else None

    def _passes(record: dict) -> bool:
        if args.ec:
            if not (record.get("ec_violation") or record.get("is_ec")):
                return False
        if args.provider:
            prov = (
                record.get("llm_provider")
                or record.get("provider")
                or record.get("provider_name")
                or ""
            )
            if prov != args.provider:
                return False
        if level_filter is not None:
            rec_level = record.get("levelno") or _level_no(record.get("level", ""))
            if rec_level < level_filter:
                return False
        return True

    if args.follow:
        # Tail -f style: print last N, then stream new lines
        all_records = list(_iter_jsonl(path))
        tail = [r for r in all_records if _passes(r)][-args.n:]
        for rec in tail:
            print(_format_record(rec))

        try:
            with open(path, encoding="utf-8") as f:
                f.seek(0, 2)  # seek to end
                while True:
                    line = f.readline()
                    if not line:
                        time.sleep(0.2)
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if _passes(rec):
                        print(_format_record(rec), flush=True)
        except KeyboardInterrupt:
            print()
    else:
        records = [r for r in _iter_jsonl(path) if _passes(r)]
        for rec in records[-args.n:]:
            print(_format_record(rec))

    return 0


# ---------------------------------------------------------------------------
# Subcommand: verify
# ---------------------------------------------------------------------------

def _cmd_verify(args: argparse.Namespace) -> int:
    try:
        from axiom.infra.audit_log import AuditLog
        al = AuditLog.get()
        result = al.verify_chain(table=args.table)
    except (ImportError, AttributeError, TypeError) as e:
        print(f"axi log verify: error calling AuditLog.verify_chain: {e}", file=sys.stderr)
        return 1

    # result may be a dict or a simple bool/status
    if isinstance(result, dict):
        ok = result.get("ok", result.get("valid", True))
        broken_at = result.get("broken_at") or result.get("index")
        if ok:
            print("OK")
            return 0
        else:
            idx = broken_at if broken_at is not None else "unknown"
            print(f"BROKEN at record index {idx}")
            return 1
    elif isinstance(result, bool):
        if result:
            print("OK")
            return 0
        else:
            print("BROKEN")
            return 1
    else:
        # Unknown result shape — print as-is
        print(str(result))
        return 0


# ---------------------------------------------------------------------------
# Subcommand: stats
# ---------------------------------------------------------------------------

def _cmd_stats(_args: argparse.Namespace) -> int:
    # Backend type
    try:
        from axiom.infra.audit_log import AuditLog
        al = AuditLog.get()
        backend_info = al.backend_info() if hasattr(al, "backend_info") else {}
        backend_name = getattr(backend_info, "backend", None) or str(type(al).__name__)
    except (ImportError, AttributeError, TypeError) as e:
        backend_name = f"(unavailable: {e})"
        backend_info = {}

    print(f"backend: {backend_name}")

    # JSONL files under runtime/logs/
    try:
        from axiom import REPO_ROOT
        logs_dir = Path(REPO_ROOT) / "runtime" / "logs"
    except Exception:
        logs_dir = None

    if logs_dir and logs_dir.is_dir():
        jsonl_files = sorted(logs_dir.glob("*.jsonl"))
        if jsonl_files:
            print("\nJSONL files:")
            for f in jsonl_files:
                count = sum(1 for _ in _iter_jsonl(f))
                print(f"  {f.name}: {count} lines")
        else:
            print("\nJSONL files: (none found)")
    else:
        print("\nJSONL files: (runtime/logs not found)")

    # EC violations and blocked counts from routing_events
    path = _routing_events_path()
    ec_count = 0
    blocked_count = 0
    if path:
        for rec in _iter_jsonl(path):
            if rec.get("ec_violation") or rec.get("is_ec"):
                ec_count += 1
            if rec.get("blocked") or rec.get("is_blocked"):
                blocked_count += 1
        print("\nrouting_events:")
        print(f"  ec_violations: {ec_count}")
        print(f"  blocked: {blocked_count}")
    else:
        print("\nrouting_events: (file not found)")

    return 0


# ---------------------------------------------------------------------------
# Subcommand: backend
# ---------------------------------------------------------------------------

def _cmd_backend(_args: argparse.Namespace) -> int:
    try:
        from axiom.infra.audit_log import AuditLog
        al = AuditLog.get()
        info = al.backend_info() if hasattr(al, "backend_info") else {}
        name = getattr(info, "backend", None) or str(type(al).__name__)
        print(name)
    except (ImportError, AttributeError, TypeError) as e:
        print(f"axi log backend: {e}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# Subcommand: sinks
# ---------------------------------------------------------------------------

def _cmd_sinks(_args: argparse.Namespace) -> int:
    try:
        from axiom.infra.log_sinks import LogSinkFactory
        sinks = LogSinkFactory.available()
    except (ImportError, AttributeError, TypeError) as e:
        print(f"axi log sinks: {e}", file=sys.stderr)
        return 1

    if not sinks:
        print("(no active log sinks)")
        return 0

    for sink in sinks:
        print(sink)

    return 0


# ---------------------------------------------------------------------------
# Subcommand: export
# ---------------------------------------------------------------------------

def _cmd_export(args: argparse.Namespace) -> int:
    path = _routing_events_path()
    if path is None:
        print("axi log export: routing_events log file not found", file=sys.stderr)
        return 1

    cutoff = _since_cutoff(args.since)

    for rec in _iter_jsonl(path):
        if cutoff is not None:
            ts = _parse_ts(rec)
            if ts is not None and ts < cutoff:
                continue
        print(json.dumps(rec))

    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def get_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser for `axi log`."""
    parser = argparse.ArgumentParser(
        prog="axi log",
        description="Log inspection, verification, and management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  axi log tail                      # Print last 20 routing events
  axi log tail --follow             # Stream new events as they arrive
  axi log tail --ec --n 50          # Last 50 EC-flagged events
  axi log tail --level warning      # Events at WARNING level or above
  axi log tail --provider llama-internal
  axi log verify                    # Verify audit chain integrity
  axi log verify --table routing    # Verify a specific table
  axi log stats                     # Show backend info and counts
  axi log backend                   # Print current backend name
  axi log sinks                     # List active log sinks
  axi log export                    # Dump all routing_events to stdout
  axi log export --since 7          # Events from the last 7 days
""",
    )

    subparsers = parser.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")
    subparsers.required = True

    # --- tail ---
    tail_p = subparsers.add_parser(
        "tail",
        help="Stream or print recent routing events",
        description="Print recent routing_events from the JSONL backend.",
    )
    tail_p.add_argument(
        "--follow", "-f",
        action="store_true",
        help="Tail the log file and stream new events as they arrive",
    )
    tail_p.add_argument(
        "--ec",
        action="store_true",
        help="Filter to records with ec_violation=True or is_ec=True",
    )
    tail_p.add_argument(
        "--level",
        metavar="LEVEL",
        help="Minimum log level (debug, info, warning, error, critical)",
    )
    tail_p.add_argument(
        "--provider",
        metavar="PROVIDER",
        help="Filter to a specific provider name",
    )
    tail_p.add_argument(
        "--n",
        type=int,
        default=20,
        metavar="N",
        help="Number of recent records to show (default: 20)",
    )
    tail_p.set_defaults(func=_cmd_tail)

    # --- verify ---
    verify_p = subparsers.add_parser(
        "verify",
        help="Verify audit log chain integrity",
        description="Call AuditLog.get().verify_chain() and report OK or BROKEN.",
    )
    verify_p.add_argument(
        "--table",
        metavar="TABLE",
        default=None,
        help="Audit table to verify (default: all)",
    )
    verify_p.add_argument(
        "--since",
        type=int,
        metavar="DAYS",
        default=None,
        help="Only check records from the last N days",
    )
    verify_p.set_defaults(func=_cmd_verify)

    # --- stats ---
    stats_p = subparsers.add_parser(
        "stats",
        help="Print backend type, file line counts, EC and blocked counts",
        description="Show backend info and summary counts from routing_events.",
    )
    stats_p.set_defaults(func=_cmd_stats)

    # --- backend ---
    backend_p = subparsers.add_parser(
        "backend",
        help="Print current audit log backend name",
        description="Print the backend name from AuditLog.get().backend_info().",
    )
    backend_p.set_defaults(func=_cmd_backend)

    # --- sinks ---
    sinks_p = subparsers.add_parser(
        "sinks",
        help="List active log sinks",
        description="Print active log sinks from LogSinkFactory.available().",
    )
    sinks_p.set_defaults(func=_cmd_sinks)

    # --- export ---
    export_p = subparsers.add_parser(
        "export",
        help="Dump routing_events JSONL to stdout",
        description="Export routing_events log records as JSONL to stdout.",
    )
    export_p.add_argument(
        "--since",
        type=int,
        metavar="DAYS",
        default=None,
        help="Only export records from the last N days",
    )
    export_p.set_defaults(func=_cmd_export)

    return parser


# ---------------------------------------------------------------------------
# Register (for CLI discovery that expects register(subparsers))
# ---------------------------------------------------------------------------

def register(subparsers: argparse._SubParsersAction) -> None:
    """Register `log` as a subcommand on the given subparsers action.

    Consumer CLI discovery calls this when noun="log" is declared in
    axiom-extension.toml and the dispatcher uses a register-based pattern.
    Adds a `log` sub-parser that delegates to the full `axi log` parser.
    """
    log_parser = get_parser()
    sub = subparsers.add_parser(
        "log",
        help=log_parser.description,
        description=log_parser.description,
    )
    sub.set_defaults(func=lambda args: main())


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Entry point called by the CLI dispatcher via mod.main()."""
    parser = get_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "func"):
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
