# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TIDY Actions-storage vitals: current artifacts vs the shared cap, caches vs the per-repo cap.

GitHub Team includes 2 GB of shared artifact + Packages storage (accrued as
GB-hours) and 10 GB of cache storage per repository. A 7-day keep on every
main ``dist`` build overflows the shared cap. This module only *reads* and
classifies; deletion is ``artifact_cleanup.StoragePolicy``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

GIB = 1024**3
INCLUDED_SHARED_GB = 2.0
INCLUDED_GB_HOURS = 1440.0  # 2 GB × 30 days × 24 hours
CACHE_CAP_GB = 10.0
WARN_PCT = 70.0
ESCALATE_PCT = 85.0
CACHE_WARN_PCT = 80.0


@dataclass(frozen=True)
class StorageReading:
    artifact_bytes: int
    cache_bytes_by_repo: dict[str, int] = field(default_factory=dict)
    accrued_gb_hours: float | None = None
    included_shared_gb: float = INCLUDED_SHARED_GB
    included_gb_hours: float = INCLUDED_GB_HOURS
    cache_cap_gb: float = CACHE_CAP_GB

    @property
    def artifact_gb(self) -> float:
        return self.artifact_bytes / GIB

    @property
    def current_pct(self) -> float:
        if self.included_shared_gb <= 0:
            return 0.0
        return (self.artifact_gb / self.included_shared_gb) * 100.0

    @property
    def accrued_pct(self) -> float | None:
        if self.accrued_gb_hours is None or self.included_gb_hours <= 0:
            return None
        return (self.accrued_gb_hours / self.included_gb_hours) * 100.0

    def cache_gb(self, repo: str) -> float:
        return (self.cache_bytes_by_repo.get(repo) or 0) / GIB

    def cache_pct(self, repo: str) -> float:
        if self.cache_cap_gb <= 0:
            return 0.0
        return (self.cache_gb(repo) / self.cache_cap_gb) * 100.0


@dataclass(frozen=True)
class ThresholdCrossing:
    kind: str  # "shared_current" | "shared_accrued" | "cache"
    severity: str  # "warn" | "escalate"
    pct: float
    summary: str
    repo: str = ""


def classify_thresholds(
    reading: StorageReading,
    *,
    warn_pct: float = WARN_PCT,
    escalate_pct: float = ESCALATE_PCT,
    cache_warn_pct: float = CACHE_WARN_PCT,
) -> list[ThresholdCrossing]:
    """Return crossings at 70% (warn) and 85% (escalate). Cache uses 80% warn, 85% escalate."""
    out: list[ThresholdCrossing] = []

    def add(kind: str, pct: float, summary: str, repo: str = "", *, cache: bool = False) -> None:
        warn = cache_warn_pct if cache else warn_pct
        if pct >= escalate_pct:
            out.append(ThresholdCrossing(kind, "escalate", pct, summary, repo))
        elif pct >= warn:
            out.append(ThresholdCrossing(kind, "warn", pct, summary, repo))

    add(
        "shared_current",
        reading.current_pct,
        (
            f"Actions shared storage is {reading.artifact_gb:.2f} GiB "
            f"({reading.current_pct:.0f}% of the {reading.included_shared_gb:.0f} GB included cap)"
        ),
    )
    if reading.accrued_pct is not None:
        add(
            "shared_accrued",
            reading.accrued_pct,
            (
                f"Actions storage accrual is {reading.accrued_gb_hours:.0f} GB-hours "
                f"({reading.accrued_pct:.0f}% of {reading.included_gb_hours:.0f} included this month)"
            ),
        )
    for repo, _size in sorted(reading.cache_bytes_by_repo.items()):
        pct = reading.cache_pct(repo)
        add(
            "cache",
            pct,
            (
                f"Actions cache in {repo} is {reading.cache_gb(repo):.2f} GiB "
                f"({pct:.0f}% of the {reading.cache_cap_gb:.0f} GB per-repo cap)"
            ),
            repo,
            cache=True,
        )
    return out


def accrued_gb_hours_from_usage_summary(payload: dict) -> float | None:
    """Sum Actions storage quantity from the billing usage/summary endpoint."""
    items = payload.get("usageItems") or []
    total = 0.0
    found = False
    for item in items:
        sku = str(item.get("sku") or "").lower()
        product = str(item.get("product") or "").lower()
        if "storage" in sku and "action" in product:
            qty = item.get("grossQuantity", item.get("quantity"))
            if isinstance(qty, (int, float)):
                total += float(qty)
                found = True
    return total if found else None
