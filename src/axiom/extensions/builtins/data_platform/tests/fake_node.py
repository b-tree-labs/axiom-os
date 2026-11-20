# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A stand-in for a node that serves only the verbs a node already served before the
everyday verbs existed: ``data.aggregate``, ``data.series`` and ``data.roles``.

It answers from an in-memory catalogue and signals, evaluating the same filter
grammar the real executor parses, so a composition that asks something the real
door would not understand fails here too. It also fails the way the real node did
when a read was shaped badly: a ``signals`` read with no window, or one that names a
channel without its feed, raises the lock timeout the colleague saw, so a test
that passes has not leaned on a read that would have timed out.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from axiom.extensions.builtins.data_platform import friendly as fr
from axiom.extensions.builtins.data_platform import gold_query as gq
from axiom.infra.skills import SkillResult

#: The verbs a node served before the everyday verbs. Anything else is a 400.
SERVED = ("data.aggregate", "data.series", "data.roles")

SITE = "example-loop"
OTHER = "other-loop"


def at(day: str, hh: int = 0, mm: int = 0) -> datetime:
    y, m, d = (int(p) for p in day.split("-"))
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


class FakeNode:
    """``signals`` rows are ``(site, feed, channel, unit, role, ts, value)``."""

    def __init__(self) -> None:
        self.signals: list[tuple] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # -- building ----------------------------------------------------------------

    def add(self, site, feed, channel, unit, role, start: datetime, hours: float, step_min: int = 10,
            base: float = 20.0) -> None:
        t, end, i = start, start + timedelta(hours=hours), 0
        while t < end:
            self.signals.append((site, feed, channel, unit, role, t, base + (i % 7) * 0.5))
            t += timedelta(minutes=step_min)
            i += 1

    @classmethod
    def standard(cls) -> FakeNode:
        """example-loop: two temperature channels with a role, a pressure channel, one channel that
        stopped early; and a second site that shares a channel name."""
        node = cls()
        for ch, base in (("T1", 20.0), ("T2", 30.0)):
            node.add(SITE, "loop.main", ch, "degC", "fluid_temperature", at("2026-03-10"), 48, base=base)
        node.add(SITE, "loop.main", "P1", "kPa", "line_pressure", at("2026-03-10"), 48, base=100.0)
        node.add(SITE, "loop.main", "Old1", "degC", None, at("2026-03-01"), 5, base=5.0)
        node.add(SITE, "loop.aux", "Dup", "degC", None, at("2026-03-10"), 2)
        node.add(SITE, "loop.main", "Dup", "degC", None, at("2026-03-10"), 2)
        node.add(OTHER, "other.main", "T1", "degC", "fluid_temperature", at("2026-03-05"), 24)
        return node

    # -- the client seam -----------------------------------------------------------

    def ask(self, target, *, verb: str, params: dict[str, Any] | None = None) -> SkillResult:
        params = dict(params or {})
        self.calls.append((verb, params))
        if verb not in SERVED:
            return SkillResult(ok=False, errors=[f"not a data read: {verb}"])
        try:
            if verb == "data.roles":
                return SkillResult(ok=True, value=self._roles())
            if verb == "data.aggregate":
                return SkillResult(ok=True, value=self._aggregate(params))
            return SkillResult(ok=True, value=self._series(params))
        except gq.GoldQueryError as exc:
            return SkillResult(ok=False, errors=[str(exc)])
        except TimeoutError as exc:
            return SkillResult(ok=False, errors=[f"LockNotAvailable: {exc}"])

    # -- the executor ---------------------------------------------------------------

    def _catalogue(self) -> list[dict[str, Any]]:
        out: dict[tuple, dict[str, Any]] = {}
        for site, feed, channel, unit, _role, ts, _v in self.signals:
            row = out.setdefault((site, feed, channel, unit), {
                "site": site, "feed": feed, "channel": channel, "unit": unit,
                "first_ts": ts, "last_ts": ts, "rows": 0})
            row["first_ts"], row["last_ts"] = min(row["first_ts"], ts), max(row["last_ts"], ts)
            row["rows"] += 1
        return list(out.values())

    def _rows(self, table: str) -> list[dict[str, Any]]:
        if table == "signal_catalogue":
            return self._catalogue()
        if table == "signals":
            names = ("site", "feed", "channel", "unit", "role", "ts", "value")
            return [dict(zip(names, r, strict=True)) for r in self.signals]
        raise gq.UnknownObject(f"no table {table!r}")

    @staticmethod
    def _match(row: dict[str, Any], clauses) -> bool:
        for c in clauses:
            have = row.get(c.column)
            if c.op == "=" and have != c.values[0]:
                return False
            if c.op == "!=" and (have is None or have == c.values[0]):
                return False  # SQL: NULL != 'x' is not true
            if c.op == "in" and have not in c.values:
                return False
        return True

    def _select(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        table = params["table"]
        clauses = gq.parse_filter(params.get("filter"))
        win = params.get("window")
        if table == "signals":
            cols = {c.column: c for c in clauses}
            if win is None or not (win.get("start") and win.get("end")):
                raise TimeoutError("canceling statement due to lock timeout (no window)")
            if "channel" in cols and "feed" not in cols:
                raise TimeoutError("canceling statement due to lock timeout (channel without its feed)")
        rows = [r for r in self._rows(table) if self._match(r, clauses)]
        if win:
            col, start, end = win["column"], fr.parse_instant(win.get("start")), fr.parse_instant(win.get("end"))
            rows = [r for r in rows if (start is None or r[col] >= start) and (end is None or r[col] < end)]
        return rows

    @staticmethod
    def _group_by(params: dict[str, Any]) -> list[str]:
        raw = params.get("group_by") or []
        return [c.strip() for c in raw.split(",")] if isinstance(raw, str) else list(raw)

    @staticmethod
    def _guard_units(rows: list[dict[str, Any]], groups: list[str]) -> None:
        """The population guard: a number over rows in two units is refused unless split by unit."""
        units = {r.get("unit") for r in rows if r.get("unit")}
        if len(units) > 1 and "unit" not in groups:
            raise gq.MixedPopulation(f"refusing to summarise across {len(units)} unit values")

    def _aggregate(self, params: dict[str, Any]) -> dict[str, Any]:
        rows, groups, col = self._select(params), self._group_by(params), params["column"]
        self._guard_units(rows, groups)
        fn = {"max": max, "min": min}[params["fn"]]
        out: dict[tuple, list] = {}
        for r in rows:
            out.setdefault(tuple(r.get(g) for g in groups), []).append(r[col])
        if not out:
            return {"data": None, "provenance": {"note": "no rows matched; no value to report"}}
        return {"data": {"table": params["table"], "column": col, "fn": params["fn"], "group_by": groups,
                         "groups": [dict(zip(groups, k, strict=True)) | {"value": fn(v), "rows": len(v),
                                                                         "matched": len(v)}
                                    for k, v in sorted(out.items(), key=lambda kv: str(kv[0]))]},
                "provenance": {"source": f"gold.{params['table']}"}}

    def _series(self, params: dict[str, Any]) -> dict[str, Any]:
        rows, groups = self._select(params), self._group_by(params)
        self._guard_units(rows, groups)
        step = fr.interval_seconds(params["bucket"].replace(" ", ""))
        buckets: dict[tuple, dict[datetime, list]] = {}
        for r in rows:
            b = datetime.fromtimestamp((r["ts"].timestamp() // step) * step, UTC)
            buckets.setdefault(tuple(r.get(g) for g in groups), {}).setdefault(b, []).append(r["value"])
        if not buckets:
            return {"data": None, "provenance": {"note": "no rows matched; no series to report"}}

        def pts(d):
            return [{"t": t, "value": sum(v) / len(v)} for t, v in sorted(d.items())]

        series = ([{"key": dict(zip(groups, k, strict=True)), "points": pts(d)} for k, d in sorted(buckets.items())]
                  if groups else pts(next(iter(buckets.values()))))
        return {"data": {"table": params["table"], "column": "value", "bucket": params["bucket"],
                         "fn": params.get("fn", "mean"), "series": series},
                "provenance": {"source": "gold.signals", "method": f"mean(value) by {params['bucket']}"}}

    def _roles(self) -> dict[str, Any]:
        by: dict[str, dict[str, Any]] = {}
        for site, _f, _c, unit, role, _t, _v in self.signals:
            if role:
                e = by.setdefault(role, {"role": role, "sites": set(), "units": set(), "rows": 0})
                e["sites"].add(site)
                e["units"].add(unit)
                e["rows"] += 1
        return {"data": {"roles": [{"role": e["role"], "sites": sorted(e["sites"]), "rows": e["rows"],
                                    "units": sorted(e["units"]), "comparable": len(e["sites"]) > 1,
                                    "note": ""} for e in by.values()]}}

    # -- assertions the tests share --------------------------------------------------

    def signal_reads(self) -> list[dict[str, Any]]:
        return [p for v, p in self.calls if p.get("table") == "signals"]
