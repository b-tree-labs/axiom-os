# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The serving tier bounds what any one call can cost (ADR-157).

One module, no database. The guard decides BEFORE a query runs (admit +
preflight), and bounds AFTER it returns (sampling + byte budget + the served
block), so the verbs stay thin and the limits live at the door. The policy is
configuration with shipped defaults; a request can never carry its own limits
(admit takes no params), and identity arrives from the caller's context, never
from anything a client typed.

Vocabulary (prd-gold-serving-safeguards):
- cost class  — what a verb is: "lookup", "aggregate", "series", "scan".
- caller class — who is asking: "agent" (MCP/tool), "interactive" (CLI, web,
  chat), "service", "anonymous" (anything unmapped; smallest budget).
- budget — default and maximum points, maximum bytes, per (cost, caller).
- limits — per-caller-class request rate, burst and concurrency.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import UTC
from pathlib import Path
from typing import Any

__all__ = [
    "Budget",
    "CallerLimits",
    "ServingGuard",
    "ServingPolicy",
    "admit",
    "bound_bytes",
    "caller_class",
    "load_policy",
    "preflight_series",
    "sample_evenly",
    "served_block",
]

# Surfaces are the transport names skill_dispatch already uses. Anything not
# named here is anonymous ON PURPOSE: a new transport must earn its budget by
# being mapped, never inherit one by accident.
_SURFACE_CLASS = {
    "mcp": "agent",
    "agent_tool": "agent",
    "cli": "interactive",
    "web": "interactive",
    "chat": "interactive",
    "runner": "service",
}

COST_CLASSES = ("lookup", "aggregate", "series", "scan")
CALLER_CLASSES = ("agent", "interactive", "service", "anonymous")


def caller_class(*, surface: str | None, assured: bool) -> str:
    """The caller class for a transport surface.

    `assured` is reserved for a later distinction between verified and open
    principals on the same surface; today the surface decides.
    """
    del assured
    if surface is None:
        return "anonymous"
    return _SURFACE_CLASS.get(surface, "anonymous")


@dataclass(frozen=True)
class Budget:
    """Points and bytes for one (cost class, caller class) pair."""

    default_points: int
    max_points: int
    max_bytes: int

    def replace(self, **over: Any) -> Budget:
        return replace(self, **over)


@dataclass(frozen=True)
class CallerLimits:
    """Request-rate and concurrency for one caller class."""

    rate_per_second: float
    burst: float
    max_concurrent: int

    def replace(self, **over: Any) -> CallerLimits:
        return replace(self, **over)


# The shipped numbers are the PRD §9.1 proposals: measured starting points to
# be tuned against real use, not settled values. Changing one is a policy-file
# edit; removing the bound is not possible through configuration.
_DEFAULT_BUDGETS: dict[str, dict[str, Budget]] = {
    "agent": {
        "lookup": Budget(200, 2_000, 64_000),
        "aggregate": Budget(200, 2_000, 64_000),
        "series": Budget(200, 2_000, 256_000),
        "scan": Budget(200, 1_000, 64_000),
    },
    "interactive": {
        "lookup": Budget(1_000, 10_000, 512_000),
        "aggregate": Budget(1_000, 10_000, 512_000),
        "series": Budget(1_000, 10_000, 2_000_000),
        "scan": Budget(500, 5_000, 512_000),
    },
    "service": {
        "lookup": Budget(5_000, 50_000, 4_000_000),
        "aggregate": Budget(5_000, 50_000, 4_000_000),
        "series": Budget(5_000, 50_000, 16_000_000),
        "scan": Budget(5_000, 50_000, 4_000_000),
    },
    "anonymous": {
        "lookup": Budget(50, 200, 16_000),
        "aggregate": Budget(50, 200, 16_000),
        "series": Budget(50, 200, 16_000),
        "scan": Budget(10, 50, 16_000),
    },
}

_DEFAULT_LIMITS: dict[str, CallerLimits] = {
    "agent": CallerLimits(1.0, 10, 4),
    "interactive": CallerLimits(5.0, 20, 8),
    "service": CallerLimits(10.0, 40, 8),
    "anonymous": CallerLimits(0.2, 3, 1),
}

# A hard ceiling on rows fetched from the database in one call, whatever the
# policy says: the backstop against a policy file edited into absurdity.
HARD_ROW_CEILING = 100_000


@dataclass(frozen=True)
class TablePolicy:
    """Per-table serving requirements a deployment declares."""

    required_filters: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rollup:
    """A deployment's declaration that a coarser table serves the same shape."""

    table: str
    native_bucket_seconds: float


_NO_TABLE_POLICY = TablePolicy()


@dataclass(frozen=True)
class ServingPolicy:
    budgets_table: dict[str, dict[str, Budget]]
    limits_table: dict[str, CallerLimits]
    suspended_principals: frozenset[str] = frozenset()
    tables_table: dict[str, TablePolicy] = field(default_factory=dict)
    rollups_table: dict[str, Rollup] = field(default_factory=dict)

    @classmethod
    def shipped_defaults(cls) -> ServingPolicy:
        return cls(
            budgets_table={k: dict(v) for k, v in _DEFAULT_BUDGETS.items()},
            limits_table=dict(_DEFAULT_LIMITS),
        )

    def budget(self, *, cost_class: str, caller_class: str) -> Budget:
        table = self.budgets_table.get(caller_class) or self.budgets_table["anonymous"]
        got = table.get(cost_class)
        if got is None:
            # An unknown cost class gets the smallest budget in the row rather
            # than a refusal here; the refusal for UNDECLARED verbs happens in
            # admit(), where the absence is a fact about the verb.
            got = min(table.values(), key=lambda b: b.max_points)
        return got

    def limits(self, caller_class: str) -> CallerLimits:
        return self.limits_table.get(caller_class) or self.limits_table["anonymous"]

    def table_policy(self, table: str) -> TablePolicy:
        return self.tables_table.get(table, _NO_TABLE_POLICY)

    def rollup(self, table: str) -> Rollup | None:
        return self.rollups_table.get(table)

    def replace(self, **over: Any) -> ServingPolicy:
        return replace(self, **over)


def load_policy(path: Path | None = None) -> ServingPolicy:
    """Shipped defaults, overlaid by an optional TOML policy file.

    The file may tighten or loosen numbers per (caller, cost) and name
    suspended principals. It cannot remove a bound: an entry it omits keeps
    the shipped default, and there is no "unlimited" spelling.
    """
    policy = ServingPolicy.shipped_defaults()
    if path is None:
        import os

        base = os.environ.get("AXIOM_CONFIG_DIR") or str(Path.home() / ".axi" / "config")
        path = Path(base) / "serving_policy.toml"
    if not Path(path).exists():
        return policy
    import tomllib

    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    budgets = {k: dict(v) for k, v in policy.budgets_table.items()}
    for caller, row in (raw.get("budgets") or {}).items():
        if caller not in budgets:
            continue
        for cost, over in row.items():
            if cost not in budgets[caller]:
                continue
            budgets[caller][cost] = budgets[caller][cost].replace(
                **{
                    k: int(v)
                    for k, v in over.items()
                    if k in ("default_points", "max_points", "max_bytes")
                }
            )
    limits = dict(policy.limits_table)
    for caller, over in (raw.get("limits") or {}).items():
        if caller not in limits:
            continue
        limits[caller] = limits[caller].replace(
            **{
                k: (float(v) if k != "max_concurrent" else int(v))
                for k, v in over.items()
                if k in ("rate_per_second", "burst", "max_concurrent")
            }
        )
    suspended = frozenset(str(s) for s in raw.get("suspended_principals", ()))
    tables: dict[str, TablePolicy] = {}
    for name, over in (raw.get("tables") or {}).items():
        tables[str(name)] = TablePolicy(
            required_filters=tuple(str(c) for c in over.get("required_filters", ()))
        )
    rollups: dict[str, Rollup] = {}
    for name, over in (raw.get("rollups") or {}).items():
        native = interval_seconds(over.get("native_bucket"))
        target = over.get("table")
        if not target or native is None:
            continue  # an unparseable rollup declares nothing
        rollups[str(name)] = Rollup(table=str(target), native_bucket_seconds=native)
    return ServingPolicy(
        budgets_table=budgets,
        limits_table=limits,
        suspended_principals=suspended,
        tables_table=tables,
        rollups_table=rollups,
    )


# ----------------------------------------------------------------- admission


@dataclass
class AdmitOutcome:
    allowed: bool
    reason: str = ""
    budget: Budget = field(default_factory=lambda: _DEFAULT_BUDGETS["anonymous"]["lookup"])
    _release: Any = None

    def release(self) -> None:
        if self._release is not None:
            self._release()
            self._release = None


class _Bucket:
    """A token bucket. Local, monotonic, thread-safe via the guard's lock."""

    def __init__(self, capacity: float, rate: float) -> None:
        self.capacity = float(capacity)
        self.rate = float(rate)
        self.tokens = float(capacity)
        self.stamp = time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class ServingGuard:
    """Admission for serving verbs: suspend, declaration, rate, concurrency.

    One instance per process. State is in-process on purpose: the limits are
    per serving process exactly as the database connections they protect are.
    """

    def __init__(self, policy: ServingPolicy) -> None:
        self.policy = policy
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}
        self._inflight: dict[str, int] = {}
        self._meter: dict[str, dict[str, int]] = {}

    def _meter_row(self, principal: str) -> dict[str, int]:
        return self._meter.setdefault(principal, {"admitted": 0, "refused": 0})

    def admit(
        self,
        *,
        verb: str,
        cost_class: str | None,
        caller_class: str,
        principal: str,
    ) -> AdmitOutcome:
        with self._lock:
            row = self._meter_row(principal)
            if principal in self.policy.suspended_principals:
                row["refused"] += 1
                return AdmitOutcome(False, f"principal {principal} is suspended")
            if not cost_class:
                row["refused"] += 1
                return AdmitOutcome(
                    False,
                    f"{verb} declares no cost class; a verb without a "
                    "declaration is refused (ADR-157)",
                )
            limits = self.policy.limits(caller_class)
            bucket = self._buckets.get(principal)
            if bucket is None:
                bucket = self._buckets[principal] = _Bucket(
                    limits.burst, limits.rate_per_second
                )
            if not bucket.take():
                row["refused"] += 1
                return AdmitOutcome(
                    False,
                    f"rate limit: {limits.rate_per_second}/s "
                    f"(burst {limits.burst:g}) for {caller_class} callers",
                )
            inflight = self._inflight.get(principal, 0)
            if inflight >= limits.max_concurrent:
                row["refused"] += 1
                return AdmitOutcome(
                    False,
                    f"too many concurrent calls: {limits.max_concurrent} "
                    f"allowed for {caller_class} callers",
                )
            self._inflight[principal] = inflight + 1
            row["admitted"] += 1
            budget = self.policy.budget(
                cost_class=cost_class, caller_class=caller_class
            )

            def _release(p: str = principal) -> None:
                with self._lock:
                    self._inflight[p] = max(0, self._inflight.get(p, 1) - 1)

            return AdmitOutcome(True, budget=budget, _release=_release)

    def metering(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {k: dict(v) for k, v in self._meter.items()}


def admit(
    guard: ServingGuard,
    *,
    verb: str,
    cost_class: str | None,
    caller_class: str,
    principal: str,
) -> AdmitOutcome:
    """Module-level door, so callers need not hold the guard type."""
    return guard.admit(
        verb=verb, cost_class=cost_class, caller_class=caller_class, principal=principal
    )


# ----------------------------------------------------------------- preflight


@dataclass(frozen=True)
class SeriesPlan:
    refused: bool = False
    reason: str = ""
    cheaper_call: str = ""
    reshaped: bool = False
    bucket_seconds: float = 0.0
    estimated_points: int = 0


def preflight_series(
    *,
    window_seconds: float | None,
    bucket_seconds: float,
    budget: Budget,
) -> SeriesPlan:
    """Estimate a series' cost from its window and bucket, before the query.

    No window means full history at the asked resolution, which is never a
    valid question to the serving tier: the refusal names the cheaper call
    instead of running the 20 seconds and refusing after (principle 9).
    An oversized ask is reshaped across the WHOLE window by widening the
    bucket, never cut to its oldest points (principle 2).
    """
    if window_seconds is None or window_seconds <= 0:
        return SeriesPlan(
            refused=True,
            reason=(
                "a series needs a time window; full history at this "
                "resolution is unbounded"
            ),
            cheaper_call=(
                "ask aggregate for the whole-history figure, or give the "
                "series a window"
            ),
        )
    bucket = max(float(bucket_seconds), 1e-9)
    estimate = window_seconds / bucket
    if estimate <= budget.max_points:
        return SeriesPlan(
            bucket_seconds=bucket, estimated_points=int(estimate) or 1
        )
    widened = window_seconds / budget.max_points
    return SeriesPlan(
        reshaped=True,
        bucket_seconds=widened,
        estimated_points=budget.max_points,
    )


# ------------------------------------------------------------------ bounding


def sample_evenly(rows: list[Any], max_items: int) -> list[Any]:
    """An even sample across the whole list, keeping both ends.

    This is the backstop when grouped results exceed the point budget after
    reshaping: the answer stays a picture of the whole window.
    """
    n = len(rows)
    if n <= max_items:
        return list(rows)
    if max_items <= 1:
        return [rows[0]]
    step = (n - 1) / (max_items - 1)
    picked = [rows[round(i * step)] for i in range(max_items)]
    picked[-1] = rows[-1]
    return picked


def bound_bytes(payload: dict, *, max_bytes: int) -> tuple[dict, str | None]:
    """Fit a result payload into a byte budget by thinning its largest list.

    Returns the (possibly reduced) payload and a note, or (payload, None)
    when it already fits. The reduction samples evenly, so what remains still
    spans what was there (principle 2), and the note says so (principle 3).
    """
    raw = json.dumps(payload, default=str)
    if len(raw) <= max_bytes:
        return payload, None

    def _largest_list(node: Any, path: tuple = ()) -> tuple[tuple, int]:
        best, size = (), 0
        if isinstance(node, list):
            if len(node) > size:
                best, size = path, len(node)
        if isinstance(node, dict):
            items = node.items()
        elif isinstance(node, list):
            items = enumerate(node)
        else:
            return best, size
        for key, value in items:
            b, s = _largest_list(value, path + (key,))
            if s > size:
                best, size = b, s
        return best, size

    import copy

    out = copy.deepcopy(payload)
    note = None
    for _ in range(12):
        raw = json.dumps(out, default=str)
        if len(raw) <= max_bytes:
            break
        path, size = _largest_list(out)
        if size <= 1:
            # nothing left to thin; cut the payload to its envelope
            out = {k: v for k, v in out.items() if k != "data"}
            out["data"] = None
            note = (
                "reduced: the result exceeded the byte budget and could not "
                "be thinned further; narrow the request"
            )
            break
        node: Any = out
        for key in path[:-1]:
            node = node[key]
        target = node[path[-1]] if path else out
        keep = max(1, int(size * max(0.1, max_bytes / max(len(raw), 1)) * 0.9))
        thinned = sample_evenly(target, keep)
        if path:
            node[path[-1]] = thinned
        note = (
            f"reduced: {size} items sampled evenly to {len(thinned)} to fit "
            f"the {max_bytes}-byte budget for this caller"
        )
    return out, note


# ----------------------------------------------- cheap-shape requirements


def missing_required_filters(
    *,
    required: tuple[str, ...],
    filter_text: str | None,
    group_by: tuple[str, ...],
    extra_columns: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Which declared index-prefix columns this request never constrains.

    Measured 2026-09-24: one key column missing made the same aggregate about
    3,000 times more expensive. A column counts as constrained when the
    filter names it (word boundary — "website" is not "site"), the request
    groups by it (the per-group plan uses the prefix), or the verb itself
    supplies it (``extra_columns``).
    """
    import re

    text = filter_text or ""
    missing = []
    for col in required:
        if col in group_by or col in extra_columns:
            continue
        if re.search(rf"\b{re.escape(col)}\b", text):
            continue
        missing.append(col)
    return tuple(missing)


def choose_rollup(
    *, policy: "ServingPolicy", table: str, bucket_seconds: float | None
) -> str | None:
    """The declared rollup table when the ask is at least as coarse as it.

    A declaration is the deployment asserting the rollup serves the same
    shape; without one, nothing changes (the negative control).
    """
    r = policy.rollup(table)
    if r is None or bucket_seconds is None:
        return None
    return r.table if bucket_seconds >= r.native_bucket_seconds else None


# ----------------------------------------------------- interval and window


_UNIT_SECONDS = {
    "microsecond": 1e-6,
    "millisecond": 1e-3,
    "second": 1.0,
    "minute": 60.0,
    "hour": 3600.0,
    "day": 86400.0,
    "week": 604800.0,
    # Calendar units, approximated FOR ESTIMATION ONLY; the database still
    # buckets them exactly. The estimate errs within 10 percent, which a
    # points budget absorbs.
    "month": 2_592_000.0,
    "year": 31_536_000.0,
}


def interval_seconds(text: str | None) -> float | None:
    """Seconds in a Postgres interval literal like ``"5 minutes"``.

    Mirrors gold_query's closed interval grammar; anything else is ``None``
    and the verb's own validation names the error.
    """
    if not text:
        return None
    import re

    m = re.match(
        r"^(\d+)\s+(microsecond|millisecond|second|minute|hour|day|week|month|year)s?$",
        str(text).strip(),
        re.I,
    )
    if not m:
        return None
    return float(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]


def format_interval(seconds: float) -> str:
    """The smallest honest interval literal covering ``seconds``.

    Used to widen a bucket after a reshape: rounding UP keeps the point count
    within budget (rounding down would overshoot it).
    """
    import math

    if seconds >= 1.0:
        return f"{math.ceil(seconds)} seconds"
    return f"{max(1, math.ceil(seconds * 1000.0))} milliseconds"


def window_seconds(window: dict | None) -> float | None:
    """The span of a verb's window param, or ``None`` when it is unbounded.

    A window with no start is unbounded into history, which for budgeting is
    the same as no window. A window with no end runs to now.
    """
    if not window:
        return None
    start, end = window.get("start"), window.get("end")
    if not start:
        return None
    from datetime import datetime

    def _parse(v: Any) -> datetime | None:
        if isinstance(v, datetime):
            return v
        try:
            return datetime.fromisoformat(str(v))
        except ValueError:
            return None

    s = _parse(start)
    if s is None:
        return None
    e = _parse(end) if end else datetime.now(tz=s.tzinfo or UTC)
    if e is None:
        return None
    span = (e - s).total_seconds()
    return span if span > 0 else None


# -------------------------------------------------------------- served block


def served_block(
    *,
    requested_window_seconds: float | None,
    requested_bucket_seconds: float | None,
    effective_bucket_seconds: float | None,
    returned_points: int,
    covered: tuple[str, str] | None,
    reduced: bool,
    cursor: str | None = None,
) -> dict:
    """What was done, stated first (principle 3).

    The block leads the envelope so a consumer that stops reading early has
    already seen requested vs returned vs covered.
    """
    block: dict[str, Any] = {
        "requested": {
            "window_seconds": requested_window_seconds,
            "bucket_seconds": requested_bucket_seconds,
        },
        "returned_points": returned_points,
        "resolution_seconds": effective_bucket_seconds,
        "reduced": reduced,
    }
    if covered is not None:
        block["covered"] = {"start": covered[0], "end": covered[1]}
    if cursor is not None:
        block["cursor"] = cursor
    return block
