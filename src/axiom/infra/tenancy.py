# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tenant offboarding — deleting a tenant, and being honest about what is left.

ADR-025 §9: if a tenant cannot be removed completely, there was never
isolation — there was a convention. That makes deletion a first-class
operation rather than a cleanup script, and it makes **coverage** the hard
part: the danger is not a delete that fails, it is a delete that succeeds
while quietly missing a subsystem nobody remembered holds tenant data.

So this is a registry with two halves:

- a :class:`TenantReaper` per subsystem that can find and remove one tenant's
  resources; and
- :data:`UNREAPED`, the **declared** list of subsystems known to hold tenant
  data with no reaper yet.

An offboard reports both. It never implies completeness it cannot demonstrate,
and the second list is the implementation checklist for the rest of ADR-025 —
written where it cannot be forgotten, because it is printed every time someone
removes a tenant.

Dry-run is the default. Deleting a facility's data is not a thing to do on a
typo.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TenantResource:
    """One thing a tenant owns, as reported before it is removed."""

    subsystem: str
    kind: str
    identifier: str
    detail: str = ""


@dataclass
class OffboardReport:
    site: str
    dry_run: bool
    found: list[TenantResource] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    unreaped: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """True only when everything registered succeeded **and** nothing is
        known to be unreaped. A tenant is not offboarded because the command
        exited zero."""
        return not self.failed and not self.unreaped

    def as_dict(self) -> dict:
        return {
            "site": self.site,
            "dry_run": self.dry_run,
            "complete": self.complete,
            "found": [r.__dict__ for r in self.found],
            "removed": self.removed,
            "failed": self.failed,
            "unreaped_subsystems": self.unreaped,
        }


@runtime_checkable
class TenantReaper(Protocol):
    """Finds and removes one tenant's resources within one subsystem."""

    subsystem: str

    def find(self, site: str) -> Iterable[TenantResource]: ...

    def reap(self, site: str) -> Iterable[str]:
        """Remove them; return one line per removal. Raise to report failure."""


class ReaperRegistry:
    def __init__(self) -> None:
        self._reapers: dict[str, TenantReaper] = {}

    def register(self, reaper: TenantReaper) -> None:
        name = getattr(reaper, "subsystem", "")
        if not name:
            raise ValueError("a reaper must name its subsystem")
        self._reapers[name] = reaper

    def subsystems(self) -> list[str]:
        return sorted(self._reapers)

    def get(self, subsystem: str) -> TenantReaper | None:
        return self._reapers.get(subsystem)

    def __len__(self) -> int:
        return len(self._reapers)


#: Subsystems known to hold tenant data that have **no reaper yet**.
#:
#: This list is the honest half of the feature. Each entry is a place a
#: tenant's data survives an offboard today, and an offboard says so out loud
#: rather than reporting success it has not earned. Entries leave this list by
#: gaining a reaper — which, per ADR-025, generally means gaining a tenant
#: dimension first (retrieval has none at all today).
UNREAPED: tuple[str, ...] = (
    # "rag" left this list when RagReaper landed — a tenant dimension made the
    # deletion expressible, and the reaper made it real.
    "caches",  # prompt/embedding/answer caches are not tenant-keyed (§2)
    "sessions",  # chat sessions and their transcripts
    "memory",  # the cross-tool memory ledger
    "evals",  # per-tenant golden sets and their results
    "traces",  # prompt logs and completions (§7)
    "bronze",  # landed rows; deletion interacts with retention policy
    "silver",  # conformed rows keyed by site, removable once retention says so
)

_REGISTRY = ReaperRegistry()


def default_registry() -> ReaperRegistry:
    return _REGISTRY


def offboard(
    site: str,
    *,
    dry_run: bool = True,
    registry: ReaperRegistry | None = None,
    unreaped: tuple[str, ...] = UNREAPED,
) -> OffboardReport:
    """Find (and unless ``dry_run``, remove) everything one tenant owns."""
    site = (site or "").strip()
    if not site:
        raise ValueError("site is required — refusing to offboard an unnamed tenant")
    reg = registry if registry is not None else default_registry()
    report = OffboardReport(site=site, dry_run=dry_run, unreaped=list(unreaped))

    for name in reg.subsystems():
        reaper = reg.get(name)
        try:
            found = list(reaper.find(site))
        except Exception as exc:  # noqa: BLE001 — one subsystem must not hide the rest
            report.failed.append(f"{name}: find failed: {type(exc).__name__}: {exc}")
            continue
        report.found.extend(found)
        if dry_run or not found:
            continue
        try:
            report.removed.extend(reaper.reap(site))
        except Exception as exc:  # noqa: BLE001
            report.failed.append(f"{name}: reap failed: {type(exc).__name__}: {exc}")
    return report


__all__ = [
    "UNREAPED",
    "OffboardReport",
    "ReaperRegistry",
    "TenantReaper",
    "TenantResource",
    "default_registry",
    "offboard",
]
