# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TRIAGE safety/integrity check primitives.

Defines the ``Finding`` shape that registered checks return, and the
sweep runner that TRIAGE's heartbeat invokes. Extensions register checks
via ``[[extension.provides]] kind = "safety_check"`` in their manifest;
this module discovers them at sweep time and runs them.

TRIAGE ships its own built-in checks via the same registration mechanism
(see ``axiom-extension.toml``) so the platform dogfoods the extension
surface and there is exactly one path to add a safety check.
"""

from __future__ import annotations

import importlib
import logging
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from axiom.infra.paths import get_user_state_dir

log = logging.getLogger(__name__)


SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"


@dataclass
class Finding:
    """One finding from a safety check.

    ``check_name`` should be ext-prefixed (e.g., ``diagnostics.disk_space``)
    so multiple extensions don't collide on a generic name like ``disk_space``.
    """

    check_name: str
    severity: str  # SEVERITY_INFO | SEVERITY_WARNING | SEVERITY_CRITICAL
    title: str
    detail: str = ""
    remediation: str = ""
    metadata: dict = field(default_factory=dict)


def _load_check_callable(entry: str):
    """Resolve a 'module.path:func' entry to a callable. Loud on failure."""
    if ":" not in entry:
        raise ValueError(f"safety_check entry must be 'module:func', got {entry!r}")
    module_path, func_name = entry.split(":", 1)
    module = importlib.import_module(module_path)
    fn = getattr(module, func_name, None)
    if fn is None or not callable(fn):
        raise ValueError(f"safety_check entry {entry!r} did not resolve to a callable")
    return fn


def run_check(name: str, entry: str, severity_default: str) -> list[Finding]:
    """Run one registered check; never raises.

    On exception, returns a single Finding describing the check itself as
    failed — a check that crashes is itself a finding TRIAGE should surface.
    """
    try:
        fn = _load_check_callable(entry)
    except Exception as exc:
        return [
            Finding(
                check_name=name,
                severity=SEVERITY_CRITICAL,
                title=f"Safety check {name!r} failed to load",
                detail=str(exc),
                remediation=f"Verify the manifest entry {entry!r} resolves to a callable.",
            )
        ]

    try:
        result = fn()
    except Exception as exc:
        return [
            Finding(
                check_name=name,
                severity=SEVERITY_CRITICAL,
                title=f"Safety check {name!r} raised an exception",
                detail=str(exc),
                remediation="Inspect the check implementation; checks must never raise.",
            )
        ]

    findings: list[Finding] = []
    if isinstance(result, Finding):
        findings.append(result)
    elif result is None:
        return findings
    else:
        try:
            for item in result:
                if isinstance(item, Finding):
                    findings.append(item)
                else:
                    findings.append(
                        Finding(
                            check_name=name,
                            severity=SEVERITY_WARNING,
                            title=f"Safety check {name!r} returned a non-Finding value",
                            detail=repr(item),
                        )
                    )
        except TypeError:
            findings.append(
                Finding(
                    check_name=name,
                    severity=SEVERITY_WARNING,
                    title=f"Safety check {name!r} returned a value that is neither Finding nor iterable",
                    detail=repr(result),
                )
            )

    # Apply severity_default to any findings missing one (defensive — checks
    # SHOULD set severity, but extension authors will forget).
    for f in findings:
        if not f.severity:
            f.severity = severity_default
    return findings


def discover_safety_checks() -> list[tuple[str, str, str]]:
    """Return ``[(name, entry, severity_default), ...]`` from all extensions."""
    from axiom.extensions.discovery import discover_extensions

    checks: list[tuple[str, str, str]] = []
    for ext in discover_extensions():
        for sc in ext.safety_checks:
            if sc.entry and sc.name:
                checks.append((sc.name, sc.entry, sc.severity_default))
    return checks


def sweep() -> dict:
    """Run every registered safety check, aggregate findings, return a dict.

    Persistence is the caller's job (the heartbeat command writes JSONL).
    Splitting these so the sweep is testable without filesystem side effects.
    """
    checks = discover_safety_checks()
    all_findings: list[Finding] = []
    counts = {SEVERITY_INFO: 0, SEVERITY_WARNING: 0, SEVERITY_CRITICAL: 0}
    for name, entry, severity_default in checks:
        for finding in run_check(name, entry, severity_default):
            all_findings.append(finding)
            counts[finding.severity] = counts.get(finding.severity, 0) + 1

    return {
        "agent": "triage",
        "ts": datetime.now(UTC).isoformat(),
        "checks_run": len(checks),
        "findings_total": len(all_findings),
        "findings_by_severity": counts,
        "findings": [
            {
                "check_name": f.check_name,
                "severity": f.severity,
                "title": f.title,
                "detail": f.detail,
                "remediation": f.remediation,
                "metadata": f.metadata,
            }
            for f in all_findings
        ],
    }


# ---------------------------------------------------------------------------
# Built-in safety checks (TRIAGE's own contributions to the registry)
# ---------------------------------------------------------------------------


def check_state_dir_disk_space() -> Iterable[Finding]:
    """Critical if state-dir partition has <1GB free; warning at <5GB.

    The state dir is where audit logs, agent knowledge stores, signed
    federation records, and learned patterns live. Running out of space
    there is a silent integrity risk — writes start failing partially,
    audit gaps appear, learned patterns get truncated. Prefer to surface
    the pressure before the partition fills.
    """
    state_dir = get_user_state_dir()
    try:
        usage = shutil.disk_usage(state_dir)
    except OSError as exc:
        return [
            Finding(
                check_name="diagnostics.state_dir_disk_space",
                severity=SEVERITY_WARNING,
                title="Could not stat state-dir partition",
                detail=f"{state_dir}: {exc}",
            )
        ]

    free_gb = usage.free / (1024**3)
    if free_gb < 1.0:
        return [
            Finding(
                check_name="diagnostics.state_dir_disk_space",
                severity=SEVERITY_CRITICAL,
                title=f"State-dir partition critically low ({free_gb:.2f} GB free)",
                detail=f"{state_dir}: {free_gb:.2f} GB free of {usage.total / (1024**3):.0f} GB",
                remediation="Free space on the state-dir partition; consider rotating audit logs.",
                metadata={"free_gb": free_gb, "total_gb": usage.total / (1024**3)},
            )
        ]
    if free_gb < 5.0:
        return [
            Finding(
                check_name="diagnostics.state_dir_disk_space",
                severity=SEVERITY_WARNING,
                title=f"State-dir partition low ({free_gb:.2f} GB free)",
                detail=f"{state_dir}: {free_gb:.2f} GB free of {usage.total / (1024**3):.0f} GB",
                remediation="Plan to free space soon; rotate audit logs or move state to a larger partition.",
                metadata={"free_gb": free_gb, "total_gb": usage.total / (1024**3)},
            )
        ]
    return []


def check_pending_patches_stale(max_age_hours: float = 24.0) -> Iterable[Finding]:
    """Warning if any patch in ~/.axi/agents/triage/patches/pending/ is older than ``max_age_hours``.

    TRIAGE-generated patches awaiting human review accumulate in this dir.
    Patches older than the threshold suggest the review queue is dropped
    on the floor.
    """
    pending_dir = get_user_state_dir() / "agents" / "triage" / "patches" / "pending"
    if not pending_dir.exists():
        return []

    findings: list[Finding] = []
    threshold_secs = max_age_hours * 3600.0
    now = datetime.now(UTC).timestamp()
    for path in pending_dir.iterdir():
        if not path.is_file():
            continue
        try:
            age_secs = now - path.stat().st_mtime
        except OSError:
            continue
        if age_secs > threshold_secs:
            findings.append(
                Finding(
                    check_name="diagnostics.pending_patches_stale",
                    severity=SEVERITY_WARNING,
                    title=f"Pending patch older than {max_age_hours:.0f}h",
                    detail=f"{path.name}: {age_secs / 3600:.1f}h old",
                    remediation=f"Review or discard pending patch: {path}",
                    metadata={"path": str(path), "age_hours": age_secs / 3600},
                )
            )
    return findings
