# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The latest value of every channel, how far to trust it, and a short trend.

What a monitoring display needs from a running producer, and nothing it does
not: per channel the current value with its unit, when it was read, how old it
is, a quality and the reason for it, and a bounded trend for a sparkline.

Quality uses ADR-132's closed vocabulary (``good``, ``suspect``, ``bad``,
``saturated``, ``stale``) so a monitor and the store never disagree about a
word. For display it maps to the three status classes operators know from
OPC UA: Good, Uncertain, Bad. The assessment, worst first:

- a value that is not a number is ``bad``;
- a declared fault rule (``company``) that condemns it is ``bad`` or ``suspect``;
- a value outside the channel's declared range is ``bad``;
- a value that stopped arriving is ``stale``;
- a value whose channel declares no unit is ``suspect`` (a value without its
  unit is not a fact);
- otherwise ``good``.

A declared channel never read is listed, ``bad``, "no reading yet": an
absent channel on a monitor is how a dead sensor goes unnoticed.

This judges what is on screen; it does not write quality to the store. That
is the validate stage's job, under the same vocabulary.
"""

from __future__ import annotations

import json
import math
import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..company import Reading, Rule, judge

#: ADR-132 quality → the OPC UA-style class a display shows.
_CLASS = {"good": "Good", "suspect": "Uncertain", "stale": "Uncertain",
          "saturated": "Uncertain", "bad": "Bad"}
_SEVERITY = {"good": 0, "stale": 1, "saturated": 2, "suspect": 3, "bad": 4}


def display_class(quality: str) -> str:
    return _CLASS.get(quality, "Bad")


@dataclass(frozen=True)
class ChannelDecl:
    unit: str | None = None
    description: str = ""
    low: float | None = None
    high: float | None = None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    return v if math.isfinite(v) else None


class ChannelSnapshot:
    def __init__(
        self,
        *,
        decls: dict[str, ChannelDecl] | None = None,
        rules: list[Rule] | None = None,
        expected_interval_s: float = 1.0,
        stale_factor: float = 5.0,
        min_stale_s: float = 30.0,
        trend_points: int = 900,
        trend_step_s: float = 1.0,
    ) -> None:
        self.decls = dict(decls or {})
        self.rules = list(rules or [])
        self.stale_after_s = max(min_stale_s, stale_factor * max(expected_interval_s, 0.001))
        self.trend_step_s = trend_step_s
        self._trend_points = trend_points
        self._latest: dict[str, dict[str, Any]] = {}
        self._trend: dict[str, deque] = {}
        self._verdicts: dict[str, tuple[str, str]] = {}

    def observe(self, feed: str, ts: Any, values: dict[str, Any], *, received_at: float) -> None:
        """Take one record's values. ``ts`` is the reading's own timestamp."""
        frame: dict[str, Reading] = {}
        for name, raw in values.items():
            if raw is None:
                continue
            num = _number(raw)
            self._latest[name] = {"feed": feed, "ts": ts, "received_at": received_at,
                                  "value": num if num is not None else raw, "numeric": num is not None}
            if num is not None:
                frame[name] = Reading(name, num, (self.decls.get(name) or ChannelDecl()).unit or "")
                buf = self._trend.setdefault(name, deque(maxlen=self._trend_points))
                if not buf or received_at - buf[-1][0] >= self.trend_step_s:
                    buf.append((received_at, num))
        if self.rules and frame:
            verdicts = {v.channel: (v.quality, v.because) for v in judge(frame, self.rules)}
            for name in frame:
                if name in verdicts:
                    self._verdicts[name] = verdicts[name]
                else:
                    self._verdicts.pop(name, None)

    def _assess(self, name: str, latest: dict[str, Any] | None, now: float) -> tuple[str, str]:
        decl = self.decls.get(name) or ChannelDecl()
        if latest is None:
            return "bad", "no reading yet"
        found: list[tuple[str, str]] = []
        if not latest["numeric"]:
            found.append(("bad", f"not a number: {latest['value']!r}"))
        else:
            v = float(latest["value"])
            if name in self._verdicts:
                found.append(self._verdicts[name])
            if (decl.low is not None and v < decl.low) or (decl.high is not None and v > decl.high):
                found.append(("bad", f"{v:g} is outside its declared range {decl.low:g} to {decl.high:g}"
                              if decl.low is not None and decl.high is not None
                              else f"{v:g} is outside its declared range"))
            if not decl.unit:
                found.append(("suspect", "no unit declared for this channel"))
        age = now - float(latest["received_at"])
        if age > self.stale_after_s:
            found.append(("stale", f"no reading for {int(age)} s"))
        if not found:
            return "good", ""
        return max(found, key=lambda qr: _SEVERITY.get(qr[0], 4))

    def view(self, *, now: float) -> dict[str, Any]:
        names = sorted(set(self.decls) | set(self._latest))
        out: dict[str, Any] = {}
        for name in names:
            latest = self._latest.get(name)
            decl = self.decls.get(name) or ChannelDecl()
            quality, reason = self._assess(name, latest, now)
            out[name] = {
                "channel": name,
                "description": decl.description,
                "unit": decl.unit,
                "value": None if latest is None else latest["value"],
                "ts": None if latest is None else latest["ts"],
                "age_s": None if latest is None else round(now - float(latest["received_at"]), 1),
                "quality": quality,
                "class": display_class(quality),
                "reason": reason,
                "feed": None if latest is None else latest["feed"],
                "trend": [list(p) for p in self._trend.get(name, ())],
            }
        counts = {"Good": 0, "Uncertain": 0, "Bad": 0}
        for row in out.values():
            counts[row["class"]] += 1
        return {"at": now, "channels": out, "counts": counts}

    def save(self, path: str | Path, *, now: float) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.view(now=now), default=str), encoding="utf-8")
        os.replace(tmp, path)


__all__ = ["ChannelDecl", "ChannelSnapshot", "display_class"]
