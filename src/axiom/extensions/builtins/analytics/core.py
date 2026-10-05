# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Deterministic series math (ADR-113 Tiers 1-3; spec-analytics-tool).

Pure, reproducible numeric operations over a series: descriptive statistics,
trend/fit, and calculus on samples. The model calls this instead of computing a
number in its head; the result carries a provenance stamp so the output-provenance
gate admits the quoted value. Tier 4 (dynamical models / ODEs) is out of scope —
that is the domain model tier (ROM), which owns the equations.

Determinism: fixed numpy methods, no randomness, no wall-clock. Same inputs +
op + params -> identical output. Every result records ``method`` (the exact
routine) so it is reproducible and auditable.
"""
from __future__ import annotations

import json
import re
from typing import Any

import numpy as np

_FLOAT_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
_DELIM_RE = re.compile(r"[,\t|;]")


class AnalyticsError(ValueError):
    """A malformed request (unknown op, missing params, unusable series)."""


# --------------------------------------------------------------------------- #
# Format-agnostic series resolution -> ordered list[float]
# --------------------------------------------------------------------------- #
def resolve_series(series: Any, column: str | int | None = None) -> list[float]:
    """Coerce any supported carrier into an ordered numeric series.

    Supports: a bare numeric list; a list-of-dicts (pick ``column`` by key);
    a dict (pick ``column`` key, else the first numeric-list value); and a
    string holding JSON, CSV/delimited text (pick ``column`` by header name or
    index; default the first numeric column), or a predominantly-numeric inline
    list. Non-numeric cells are dropped.
    """
    if series is None:
        return []
    if isinstance(series, str):
        s = series.strip()
        if s[:1] in "[{":
            try:
                return resolve_series(json.loads(s), column)
            except (ValueError, RecursionError):
                pass
        return _series_from_text(s, column)
    if isinstance(series, dict):
        if column is not None:
            if column in series:
                return _to_floats(series[column])
            raise AnalyticsError(
                f"no series {column!r} in the data (have: {', '.join(map(str, series))}); "
                "refusing to substitute a different one"
            )
        for v in series.values():
            if isinstance(v, list):
                got = _to_floats(v)
                if got:
                    return got
        return []
    if isinstance(series, (list, tuple)):
        if series and all(isinstance(x, dict) for x in series):
            key = column
            if key is None:  # first key whose column is numeric
                for k in series[0]:
                    col = _to_floats([row.get(k) for row in series])
                    if col:
                        return col
                return []
            if not any(key in row for row in series):
                keys = sorted({k for row in series for k in row})
                raise AnalyticsError(
                    f"no column {key!r} in the rows (have: {', '.join(map(str, keys))}); "
                    "refusing to substitute a different one"
                )
            return _to_floats([row.get(key) for row in series])
        return _to_floats(series)
    return []


def _to_floats(seq: Any) -> list[float]:
    out: list[float] = []
    if not isinstance(seq, (list, tuple)):
        return out
    for x in seq:
        if isinstance(x, bool):
            continue
        if isinstance(x, (int, float)):
            out.append(float(x))
        elif isinstance(x, str):
            try:
                out.append(float(x.strip().rstrip("%")))
            except ValueError:
                continue
    return out


def _series_from_text(text: str, column: str | int | None) -> list[float]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    rows = [
        [c.strip() for c in (_DELIM_RE.split(ln) if _DELIM_RE.search(ln) else ln.split())]
        for ln in lines
    ]
    # Columnar (>=2 rows): choose the column, else the first numeric column.
    if len(rows) >= 2 and max(len(r) for r in rows) >= 1:
        header = rows[0]
        col_idx: int | None = None
        data_start = 0
        width_all = max(len(r) for r in rows)
        if isinstance(column, int):
            if not -width_all <= column < width_all:
                raise AnalyticsError(
                    f"column index {column} is out of range (row width {width_all}); "
                    "refusing to substitute a different column"
                )
            col_idx = column
        elif isinstance(column, str):
            if column in header:
                col_idx = header.index(column)
                data_start = 1
            else:
                # Falling through here would silently average "the first numeric
                # column" — answering a question the caller did not ask, with a
                # number that looks real. A named column must exist.
                raise AnalyticsError(
                    f"no column {column!r} in the header (have: {', '.join(header)}); "
                    "refusing to substitute a different column"
                )
        if col_idx is not None:
            vals = _to_floats([r[col_idx] for r in rows[data_start:] if col_idx < len(r)])
            if vals:
                return vals
        # default: first column that is numeric across the data rows
        width = max(len(r) for r in rows)
        for c in range(width):
            for ds in (0, 1):  # try with and without a header row
                vals = _to_floats([r[c] for r in rows[ds:] if c < len(r)])
                if len(vals) >= 2:
                    return vals
    # Inline single line: the caller passed this explicitly as the series, so take
    # every numeric token (a leading label like "peaks:" must not eat the first value).
    if len(rows) == 1:
        return [float(m) for m in _FLOAT_RE.findall(lines[0])]
    return []


# --------------------------------------------------------------------------- #
# Operations (op -> (callable(vals, params) -> value, method-name))
# --------------------------------------------------------------------------- #
def _need(vals: list[float], n: int = 1) -> None:
    if len(vals) < n:
        raise AnalyticsError(f"need at least {n} numeric point(s), got {len(vals)}")


def _r2(y: np.ndarray, yhat: np.ndarray) -> float:
    ss_res = float(np.sum((y - yhat) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    return 1.0 - ss_res / ss_tot if ss_tot else 1.0


def _linregress(vals: list[float], p: dict) -> dict:
    _need(vals, 2)
    y = np.asarray(vals, dtype=float)
    x = np.asarray(p.get("x") or list(range(len(vals))), dtype=float)
    if len(x) != len(y):
        raise AnalyticsError("x and y must be the same length")
    slope, intercept = np.polyfit(x, y, 1)
    return {
        "slope": float(slope),
        "intercept": float(intercept),
        "r2": _r2(y, slope * x + intercept),
    }


def _polyfit(vals: list[float], p: dict) -> dict:
    deg = int(p.get("degree", 2))
    _need(vals, deg + 1)
    y = np.asarray(vals, dtype=float)
    x = np.asarray(p.get("x") or list(range(len(vals))), dtype=float)
    coeffs = np.polyfit(x, y, deg)
    return {
        "coeffs": [float(c) for c in coeffs],
        "degree": deg,
        "r2": _r2(y, np.polyval(coeffs, x)),
    }


def _moving_average(vals: list[float], p: dict) -> dict:
    w = int(p.get("window", 3))
    _need(vals, w)
    kernel = np.ones(w) / w
    ma = np.convolve(np.asarray(vals, dtype=float), kernel, mode="valid")
    return {"window": w, "values": [float(v) for v in ma]}


def _rate_of_change(vals: list[float], p: dict) -> dict:
    _need(vals, 2)
    a = np.asarray(vals, dtype=float)
    diffs = np.diff(a)
    return {
        "mean_delta_per_step": float((a[-1] - a[0]) / (len(a) - 1)),
        "values": [float(d) for d in diffs],
    }


def _correlation(vals: list[float], p: dict) -> dict:
    y = np.asarray(vals, dtype=float)
    x = np.asarray(p.get("x") or [], dtype=float)
    if len(x) != len(y) or len(y) < 2:
        raise AnalyticsError("correlation needs equal-length x and y (>=2 points)")
    return {"pearson_r": float(np.corrcoef(x, y)[0, 1])}


_SCALAR_OPS = {
    "sum": (lambda v, p: float(np.sum(v)), "numpy.sum"),
    "mean": (lambda v, p: float(np.mean(v)), "numpy.mean"),
    "min": (lambda v, p: float(np.min(v)), "numpy.min"),
    "max": (lambda v, p: float(np.max(v)), "numpy.max"),
    "std": (lambda v, p: float(np.std(v)), "numpy.std(ddof=0)"),
    "var": (lambda v, p: float(np.var(v)), "numpy.var(ddof=0)"),
    "median": (lambda v, p: float(np.median(v)), "numpy.median"),
    "range": (lambda v, p: float(np.max(v) - np.min(v)), "max-min"),
    "count": (lambda v, p: len(v), "len"),
    "percentile": (
        lambda v, p: float(np.percentile(v, float(p["q"]), method="linear")),
        "numpy.percentile(linear)",
    ),
    "integral": (lambda v, p: float(np.trapezoid(v, dx=float(p.get("dx", 1.0)))), "numpy.trapezoid"),
    "interpolate": (
        lambda v, p: float(np.interp(float(p["at"]), list(range(len(v))), v)),
        "numpy.interp",
    ),
}
_SERIES_OPS = {
    "cumsum": (lambda v, p: [float(x) for x in np.cumsum(v)], "numpy.cumsum"),
    "derivative": (lambda v, p: [float(x) for x in np.gradient(np.asarray(v, float))], "numpy.gradient"),
}
_STRUCT_OPS = {
    "linregress": (_linregress, "numpy.polyfit(deg=1)+r2"),
    "polyfit": (_polyfit, "numpy.polyfit+r2"),
    "moving_average": (_moving_average, "convolution-mean"),
    "rate_of_change": (_rate_of_change, "numpy.diff"),
    "correlation": (_correlation, "numpy.corrcoef"),
}

OPS: tuple[str, ...] = tuple(sorted({*_SCALAR_OPS, *_SERIES_OPS, *_STRUCT_OPS}))


def compute(op: str, series: Any, *, column: str | int | None = None, params: dict | None = None,
            source: str | None = None) -> dict:
    """Run ``op`` over ``series`` and return a provenance-stamped result dict.

    Returns ``{op, value|values|<struct fields>, n, params, method, source, series}``.
    An empty/unusable series returns ``{value: None, reason: ...}`` (never a made-up
    number). Raises :class:`AnalyticsError` on an unknown op or missing param.
    """
    p = dict(params or {})
    vals = resolve_series(series, column)
    stamp = {
        "op": op,
        "n": len(vals),
        "params": p,
        "source": source,
        "series": column if column is not None else "inline",
    }
    if op in _SCALAR_OPS and op != "count" and not vals:
        return {**stamp, "value": None, "method": _SCALAR_OPS[op][1],
                "reason": "empty or non-numeric series — no data"}
    if op in _SCALAR_OPS:
        fn, method = _SCALAR_OPS[op]
        if op not in ("count",):
            _need(vals, 1)
        return {**stamp, "value": fn(vals, p), "method": method}
    if op in _SERIES_OPS:
        fn, method = _SERIES_OPS[op]
        if not vals:
            return {**stamp, "values": [], "method": method, "reason": "empty series — no data"}
        return {**stamp, "values": fn(vals, p), "method": method}
    if op in _STRUCT_OPS:
        fn, method = _STRUCT_OPS[op]
        result = fn(vals, p)
        return {**stamp, **result, "method": method}
    raise AnalyticsError(f"unknown op {op!r}; supported: {', '.join(OPS)}")
