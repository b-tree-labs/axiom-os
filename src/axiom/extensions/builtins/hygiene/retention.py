# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Retention policy engine — configurable, auditable data lifecycle management.

Loads policies from runtime/config/retention.yaml, scans state locations for
files past their retention cutoff, and logs all actions to a JSONL audit trail.

Integrated into TIDY's periodic sweep cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from axiom.infra.state import STATE_LOCATIONS, locked_append_jsonl

logger = logging.getLogger(__name__)


@dataclass
class RetentionPolicy:
    """A single retention rule from retention.yaml."""

    key: str
    days: int
    after: str  # "processed" | "ingested" | "created" | "last_accessed"


@dataclass
class RetentionAction:
    """A planned or executed retention action on a single file."""

    path: Path
    policy_key: str
    age_days: int
    action: str  # "delete" | "skip"
    reason: str  # "retention_policy" | "legal_hold"
    size_bytes: int = 0


def load_retention_config(
    config_dir: Path,
    example_dir: Path | None = None,
) -> tuple[list[RetentionPolicy], bool, Path]:
    """Load retention config from config_dir, falling back to example_dir.

    Returns:
        (policies, legal_hold_enabled, audit_log_path)
    """
    from axiom.infra.config_loader import load_yaml

    config_path = config_dir / "retention.yaml"
    if not config_path.exists() and example_dir is not None:
        config_path = example_dir / "retention.yaml"

    default_audit = config_dir.parent / "logs" / "retention_audit.jsonl"
    cfg = load_yaml(config_path)
    if not cfg:
        return [], False, default_audit

    policies: list[RetentionPolicy] = []
    for key, val in cfg.get("retention", {}).items():
        if isinstance(val, dict) and "days" in val:
            policies.append(RetentionPolicy(
                key=key,
                days=val["days"],
                after=val.get("after", "created"),
            ))

    legal_hold = cfg.get("legal_hold", {}).get("enabled", False)
    audit_path = Path(cfg.get("audit", {}).get(
        "log_path", "runtime/logs/retention_audit.jsonl",
    ))
    return policies, legal_hold, audit_path


def _get_file_age_reference(path: Path, after: str) -> datetime:
    """Determine the reference timestamp for retention calculation."""
    stat = path.stat()
    if after == "last_accessed":
        return datetime.fromtimestamp(stat.st_atime, tz=UTC)
    # "processed", "ingested" use mtime; "created" uses ctime
    if after == "created":
        # On macOS st_birthtime is real creation; elsewhere ctime is close enough
        birth = getattr(stat, "st_birthtime", None)
        if birth is not None:
            return datetime.fromtimestamp(birth, tz=UTC)
        return datetime.fromtimestamp(stat.st_ctime, tz=UTC)
    # Default: mtime (processed, ingested)
    return datetime.fromtimestamp(stat.st_mtime, tz=UTC)


def scan_retention(
    project_root: Path,
    policies: list[RetentionPolicy],
    legal_hold: bool,
) -> list[RetentionAction]:
    """Scan state locations and identify files past retention cutoff.

    Returns a list of RetentionAction describing what should (or would) happen.
    """
    now = datetime.now(UTC)
    actions: list[RetentionAction] = []
    policy_map = {p.key: p for p in policies}

    for loc in STATE_LOCATIONS:
        if loc.retention_key is None or loc.retention_key not in policy_map:
            continue

        policy = policy_map[loc.retention_key]
        cutoff = now - timedelta(days=policy.days)
        loc_path = project_root / loc.path

        if not loc_path.exists():
            continue

        # Collect files to check
        files: list[Path] = []
        if loc_path.is_dir():
            files = list(loc_path.glob(loc.glob_pattern))
        elif loc_path.is_file():
            files = [loc_path]

        for file_path in files:
            if not file_path.is_file():
                continue
            try:
                ref_time = _get_file_age_reference(file_path, policy.after)
            except OSError:
                continue

            age_days = (now - ref_time).days
            if ref_time < cutoff:
                try:
                    size = file_path.stat().st_size
                except OSError:
                    size = 0
                action = "skip" if legal_hold else "delete"
                reason = "legal_hold" if legal_hold else "retention_policy"
                actions.append(RetentionAction(
                    path=file_path,
                    policy_key=policy.key,
                    age_days=age_days,
                    action=action,
                    reason=reason,
                    size_bytes=size,
                ))

    return actions


def execute_retention(
    actions: list[RetentionAction],
    audit_path: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute retention actions and log to audit trail.

    Returns summary dict: {"deleted": N, "skipped": N, "bytes_freed": N, "errors": N}
    """
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"deleted": 0, "skipped": 0, "bytes_freed": 0, "errors": 0}

    for action in actions:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "action": "dry_run" if dry_run else action.action,
            "path": str(action.path),
            "reason": action.reason,
            "policy": action.policy_key,
            "age_days": action.age_days,
            "size_bytes": action.size_bytes,
        }

        if action.action == "delete" and not dry_run:
            try:
                action.path.unlink()
                summary["deleted"] += 1
                summary["bytes_freed"] += action.size_bytes
            except OSError as exc:
                entry["action"] = "error"
                entry["error"] = str(exc)
                summary["errors"] += 1
        elif action.action == "skip":
            summary["skipped"] += 1

        # Append to audit log
        try:
            locked_append_jsonl(audit_path, entry)
        except OSError:
            pass

    return summary


def retention_status(
    project_root: Path,
    policies: list[RetentionPolicy],
    legal_hold: bool,
) -> dict[str, Any]:
    """Generate a human-readable retention status report.

    Returns dict with per-category summaries and totals.
    """
    actions = scan_retention(project_root, policies, legal_hold)

    # Group by policy key
    by_policy: dict[str, list[RetentionAction]] = {}
    for a in actions:
        by_policy.setdefault(a.policy_key, []).append(a)

    categories: list[dict[str, Any]] = []
    total_files = 0
    total_bytes = 0

    policy_map = {p.key: p for p in policies}
    for key, policy_actions in sorted(by_policy.items()):
        policy = policy_map.get(key)
        cat_bytes = sum(a.size_bytes for a in policy_actions)
        categories.append({
            "policy_key": key,
            "days": policy.days if policy else 0,
            "after": policy.after if policy else "unknown",
            "files": len(policy_actions),
            "bytes": cat_bytes,
            "actions": policy_actions,
        })
        total_files += len(policy_actions)
        total_bytes += cat_bytes

    return {
        "categories": categories,
        "total_files": total_files,
        "total_bytes": total_bytes,
        "legal_hold": legal_hold,
    }
