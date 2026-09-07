# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TIDY Local Sweep Health — closes the test-suite-health gap surfaced 2026-05-03.

The remote-only CI watcher (`ci_watcher.py`) cannot detect failures that
exist in the working tree but haven't been pushed yet. The pre-push hook
catches them, but only when a human pushes — sustained failures can sit
on `main` for days before anyone notices.

This module gives TIDY the eyes to detect that condition without re-running
the full sweep on every heartbeat. It reads `.pytest_cache/v/cache/lastfailed`,
which pytest maintains automatically across all invocations (including the
pre-push hook), and assesses staleness by comparing cache mtime against
recent changes under `src/` and `tests/`.

Per Coverage Manifest §4.1 (`spec-agent-coverage-manifest.md`), this module
implements detection for the entry "Local sweep has ≥N sustained failures."
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

Severity = Literal["info", "warn", "escalate"]
Source = Literal["pytest-lastfailed", "no-cache", "stale-cache"]

# Default threshold — sustained failures above this trigger `escalate`.
# Below it, only `warn`. Tuned to allow a small flake window without
# flooding the user with proposals.
DEFAULT_THRESHOLD = 10


@dataclass
class LocalSweepHealth:
    """Health of the local pytest sweep, derived from pytest's own cache."""

    failure_count: int = 0
    """Number of failing test ids in `.pytest_cache/v/cache/lastfailed`."""

    cached_at: datetime | None = None
    """When the cache was last written (file mtime)."""

    source: Source = "no-cache"
    """Where the signal came from."""

    severity: Severity = "info"
    """Manifest severity (per spec-agent-coverage-manifest.md §4.3)."""

    recent_failures: list[str] = field(default_factory=list)
    """Failing test ids (capped at 20 for log hygiene; see `_truncate`)."""

    is_stale: bool = False
    """True if source files under src/ or tests/ are newer than the cache."""

    threshold: int = DEFAULT_THRESHOLD
    """Threshold above which severity escalates."""

    @property
    def healthy(self) -> bool:
        return self.failure_count == 0 and self.source != "no-cache"

    def to_dict(self) -> dict:
        return {
            "failure_count": self.failure_count,
            "cached_at": self.cached_at.isoformat() if self.cached_at else None,
            "source": self.source,
            "severity": self.severity,
            "recent_failures": self.recent_failures,
            "is_stale": self.is_stale,
            "threshold": self.threshold,
            "healthy": self.healthy,
        }


def assess_local_sweep(
    repo_dir: Path,
    threshold: int = DEFAULT_THRESHOLD,
) -> LocalSweepHealth:
    """Assess the local pytest sweep without running tests.

    Reads pytest's lastfailed cache; classifies severity based on count
    and freshness. Returns `info` (no-cache) when pytest has never run —
    no false-positive escalation on a fresh checkout.
    """
    cache_path = _find_lastfailed_cache(repo_dir)
    if cache_path is None or not cache_path.exists():
        return LocalSweepHealth(
            source="no-cache",
            severity="info",
            threshold=threshold,
        )

    try:
        cached_at = datetime.fromtimestamp(cache_path.stat().st_mtime, tz=UTC)
        with cache_path.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("local_sweep: failed to read pytest cache at %s: %s", cache_path, e)
        return LocalSweepHealth(source="no-cache", severity="info", threshold=threshold)

    # pytest stores lastfailed as {test_id: True}
    failures = sorted(data.keys()) if isinstance(data, dict) else []
    is_stale = _cache_is_stale(cache_path, repo_dir)

    severity = _classify_severity(
        failure_count=len(failures),
        is_stale=is_stale,
        threshold=threshold,
    )

    source: Source = "stale-cache" if is_stale else "pytest-lastfailed"

    return LocalSweepHealth(
        failure_count=len(failures),
        cached_at=cached_at,
        source=source,
        severity=severity,
        recent_failures=_truncate(failures),
        is_stale=is_stale,
        threshold=threshold,
    )


def _classify_severity(
    failure_count: int,
    is_stale: bool,
    threshold: int,
) -> Severity:
    """Map (count, staleness) → severity per spec-agent-coverage-manifest.md §4.3.

    - 0 failures, fresh cache → info (healthy)
    - 0 failures, stale cache → info (no signal of regression)
    - >0 failures, stale cache → warn (might already be fixed)
    - 1..threshold failures, fresh cache → warn
    - >threshold failures, fresh cache → escalate (the originating gap)
    """
    if failure_count == 0:
        return "info"
    if is_stale:
        return "warn"
    if failure_count >= threshold:
        return "escalate"
    return "warn"


def _find_lastfailed_cache(repo_dir: Path) -> Path | None:
    """Locate pytest's lastfailed cache. pytest writes it to:
    `<rootdir>/.pytest_cache/v/cache/lastfailed`. Repo root is the typical
    rootdir for this codebase.
    """
    candidate = repo_dir / ".pytest_cache" / "v" / "cache" / "lastfailed"
    if candidate.exists():
        return candidate
    # Some setups put the cache in tests/ or src/. Check both.
    for sub in ("tests", "src"):
        alt = repo_dir / sub / ".pytest_cache" / "v" / "cache" / "lastfailed"
        if alt.exists():
            return alt
    return candidate  # return canonical path even if missing — caller checks


def _cache_is_stale(cache_path: Path, repo_dir: Path) -> bool:
    """True if any source file under src/ or tests/ is newer than the cache.

    A stale cache means the lastfailed entries reference test runs that
    pre-date subsequent code changes, so the failures may already be fixed.
    """
    try:
        cache_mtime = cache_path.stat().st_mtime
    except OSError:
        return False

    for root in ("src", "tests"):
        root_path = repo_dir / root
        if not root_path.exists():
            continue
        for path in root_path.rglob("*.py"):
            try:
                if path.stat().st_mtime > cache_mtime:
                    return True
            except OSError:
                continue
    return False


def _truncate(items: list[str], limit: int = 20) -> list[str]:
    """Cap recent failures list. Avoids log spam when N is large."""
    if len(items) <= limit:
        return items
    return items[:limit] + [f"... (+{len(items) - limit} more)"]


def run_local_sweep_cycle(repo_dir: Path) -> LocalSweepHealth:
    """Called from TIDY's heartbeat. Logs at the appropriate severity.

    Returns the health record so callers (CI summary, RACI proposal) can
    chain on it.
    """
    health = assess_local_sweep(repo_dir)

    if health.severity == "escalate":
        log.warning(
            "LOCAL SWEEP RED: %d failures (threshold=%d, source=%s). "
            "Coverage Manifest entry should escalate via RACI.",
            health.failure_count,
            health.threshold,
            health.source,
        )
    elif health.severity == "warn":
        log.info(
            "Local sweep has %d failures (source=%s, stale=%s). Watching.",
            health.failure_count,
            health.source,
            health.is_stale,
        )
    elif health.healthy:
        log.debug("Local sweep healthy (cached_at=%s).", health.cached_at)

    return health
