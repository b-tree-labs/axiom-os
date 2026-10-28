# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Month-to-date Actions spend, its run-rate projection, and the budget share.

Built from billing usage items: one row per day, repository and SKU, carrying
``quantity`` (minutes for compute SKUs), ``grossAmount`` and ``netAmount``.
"""

from __future__ import annotations

import calendar
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from axiom.extensions.builtins.release.ci_health.client import CIClient, CIClientError
from axiom.extensions.builtins.release.ci_health.config import CIHealthConfig


@dataclass(frozen=True)
class SpendReading:
    year: int
    month: int
    day: int
    days_in_month: int
    minutes: float
    gross: float
    net: float
    #: The amount compared with the budget (net or gross, per config).
    spent: float
    budget: float | None
    projected: float
    last_month: float | None
    by_repo: dict[str, dict[str, float]] = field(default_factory=dict)
    by_sku: dict[str, float] = field(default_factory=dict)
    error: str = ""

    @property
    def pct(self) -> float | None:
        if not self.budget:
            return None
        return 100.0 * self.spent / self.budget

    @property
    def projected_pct(self) -> float | None:
        if not self.budget:
            return None
        return 100.0 * self.projected / self.budget

    @property
    def run_rate_multiple(self) -> float | None:
        if not self.last_month:
            return None
        return self.projected / self.last_month

    def to_dict(self) -> dict:
        data = asdict(self)
        data.update(pct=self.pct, projected_pct=self.projected_pct, run_rate_multiple=self.run_rate_multiple)
        return data


def _actions(items: list[dict]) -> list[dict]:
    return [item for item in items if str(item.get("product") or "").lower() == "actions"]


def _amount(item: dict, key: str) -> float:
    value = item.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _is_minutes(item: dict) -> bool:
    return str(item.get("unitType") or "").lower() == "minutes"


def summarize(items: list[dict], basis: str = "net") -> dict:
    """Totals and per-repo / per-SKU splits for one month of usage items."""
    rows = _actions(items)
    by_repo: dict[str, dict[str, float]] = {}
    by_sku: dict[str, float] = {}
    minutes = gross = net = 0.0
    for item in rows:
        g = _amount(item, "grossAmount")
        n = _amount(item, "netAmount")
        gross += g
        net += n
        sku = str(item.get("sku") or "unknown")
        by_sku[sku] = by_sku.get(sku, 0.0) + (n if basis == "net" else g)
        repo = str(item.get("repositoryName") or "unknown")
        slot = by_repo.setdefault(repo, {"minutes": 0.0, "gross": 0.0, "net": 0.0})
        slot["gross"] += g
        slot["net"] += n
        if _is_minutes(item):
            q = _amount(item, "quantity")
            minutes += q
            slot["minutes"] += q
    return {"minutes": minutes, "gross": gross, "net": net, "by_repo": by_repo, "by_sku": by_sku}


def project(spent: float, day: int, days_in_month: int, *, elapsed_fraction: float | None = None) -> float:
    """Linear run-rate to month end. ``day`` is the 1-based day of month."""
    elapsed = elapsed_fraction if elapsed_fraction is not None else max(day, 1) - 1 + 0.5
    elapsed = max(elapsed, 0.5)
    return spent * days_in_month / elapsed


def forge_budget(rows: list[dict]) -> float | None:
    for row in rows:
        sku = str(row.get("budget_product_sku") or row.get("budget_product_skus") or "").lower()
        if "actions" in sku and isinstance(row.get("budget_amount"), (int, float)):
            return float(row["budget_amount"])
    return None


def read_spend(client: CIClient, config: CIHealthConfig, org: str, *, now: datetime | None = None) -> SpendReading:
    """One reading. A failed billing read comes back with ``error`` set."""
    now = now or datetime.now(UTC)
    year, month = now.year, now.month
    days = calendar.monthrange(year, month)[1]
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    try:
        current = summarize(client.billing_usage(org, year, month), config.budget_basis)
    except CIClientError as exc:
        return SpendReading(year, month, now.day, days, 0, 0, 0, 0, None, 0, None, error=str(exc))
    try:
        previous = summarize(client.billing_usage(org, prev_year, prev_month), config.budget_basis)
        last = previous["net" if config.budget_basis == "net" else "gross"]
    except CIClientError:
        last = None
    spent = current["net" if config.budget_basis == "net" else "gross"]
    budget = config.budget_dollars if config.budget_dollars > 0 else forge_budget(client.budgets(org))
    elapsed = (now.day - 1) + (now.hour * 60 + now.minute) / 1440.0
    return SpendReading(
        year=year,
        month=month,
        day=now.day,
        days_in_month=days,
        minutes=round(current["minutes"], 1),
        gross=round(current["gross"], 2),
        net=round(current["net"], 2),
        spent=round(spent, 2),
        budget=budget,
        projected=round(project(spent, now.day, days, elapsed_fraction=elapsed), 2),
        last_month=round(last, 2) if last is not None else None,
        by_repo={k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in current["by_repo"].items()},
        by_sku={k: round(v, 2) for k, v in current["by_sku"].items()},
    )
