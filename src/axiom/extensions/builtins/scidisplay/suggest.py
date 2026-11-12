# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Which charts a result can honestly be drawn as, best first (ADR-215).

A person who asked a question about data should not also have to know which
picture answers it. This module reads the data an answer already holds and
says which ChartSpec 2.0 kinds can draw it, how each one is bound, and which
two or three are worth showing first. It is three steps, and only the last
one is a matter of judgement:

1. **Profile** (:func:`profile`). What the result is: its fields and their
   roles (time, quantitative with a unit, categorical with a cardinality,
   ordinal, grid, hierarchy), how many rows and series, the sign and range of
   the values, the cadence and sparsity of a time axis, and any reference a
   value is measured against (a limit, a target, a percentage's 100).
2. **Compatibility** (:func:`compatible`). A deterministic function from the
   profile to the kinds that can draw it, each with the reason it can or
   cannot. A pie of negative values, a gauge with no limit, a histogram of
   six readings: these are refused here, by rule, with the rule stated. The
   table of rules is :data:`RULES` and it is the table in ADR-215.
3. **Ranking** (:func:`rank`). The question is classified into intents
   (:func:`intents`): a trend, a comparison, part of a whole, a distribution,
   a relationship, a status against a limit, a ranking, a breakdown, a
   composition over time. Each intent prefers some kinds. The valid kinds are
   ordered by that preference, then by a fixed order, so the same question on
   the same data always ranks the same. A model may reorder WITHIN the valid
   set (``prefer``) and can never add a kind to it.

:func:`suggest` does all three and builds a ChartSpec 2.0 for every valid
kind, with the data bound and decimated to the width it will be drawn at.

The module is pure: no store, no network, no renderer. It names no domain
noun; a site, a channel and a unit are strings it carries.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import unified_spec as us

# ---------------------------------------------------------------------------
# Thresholds. Each is a rule in ADR-215's table; change them there too.
# ---------------------------------------------------------------------------

#: A pie or donut is read by comparing angles; past this many slices it is not.
PIE_MAX_SLICES = 8
#: Bars stay legible to about this many categories (or buckets of time).
BAR_MAX_CATEGORIES = 40
#: Below this many values a histogram's bins are mostly empty or single.
HISTOGRAM_MIN_VALUES = 20
#: A box summarises quartiles; fewer values than this and it summarises noise.
BOX_MIN_VALUES = 5
#: More boxes than this side by side are a texture, not a comparison.
BOX_MAX_GROUPS = 24
#: A scatter needs this many paired readings before a relationship is visible.
SCATTER_MIN_PAIRS = 5
#: KPI tiles: one per series, and a strip of more than this is a table.
KPI_MAX_SERIES = 6
#: A line needs two points; a time axis needs two distinct instants.
LINE_MIN_POINTS = 2
#: A heatmap of a time axis by series needs at least this many series rows.
HEATMAP_MIN_ROWS = 3
#: A categorical column with more distinct values than this is an identity.
CATEGORICAL_MAX = 60
#: A state (a label that changes over time) has few distinct values.
STATE_MAX_LABELS = 12

#: Parts a stacked bar keeps by name before the rest become one "other".
STACK_MAX = 8
#: The most suggestions offered up front; the rest are "more".
TOP = 3
#: Points per series an inline chart is decimated to, per pixel of width.
POINTS_PER_PX = 0.5
#: The width an inline chart is drawn at when the caller names none.
DEFAULT_WIDTH = 720
#: The bytes the bound data of one suggestion block may take inline. Past this
#: the block is decimated harder, and past that the caller sends a data ref.
DATA_CAP_BYTES = 96 * 1024

#: Units that are intensities, not amounts: averaging them is fine, adding them
#: is not. Parts of a whole, and an area filled to zero, assume an amount.
INTENSIVE_UNITS = frozenset({
    "degc", "degf", "degk", "k", "°c", "°f", "c", "f", "%", "percent", "pct",
    "ratio", "fraction", "ph", "db", "dbm", "pcm",
})
#: Units that say "a share of a reference of 100".
PERCENT_UNITS = frozenset({"%", "percent", "pct"})

#: Every kind this module knows a rule for, in its fixed tie-break order.
KIND_ORDER: tuple[str, ...] = (
    "line", "bar", "scatter", "area", "histogram", "box", "step", "pie", "donut",
    "heatmap", "gauge", "bullet", "progress", "kpi", "sparkline", "state", "flame",
    "mesh3d", "surface", "scatter3d", "volume",
)

#: Variants are kinds drawn with an option that changes what they can honestly
#: draw: a second value axis, or one panel per unit. They are chips like kinds.
VARIANTS: dict[str, tuple[str, str]] = {
    "line-dual": ("line", "dual axis"),
    "line-panels": ("line", "one panel per unit"),
    "bar-stacked": ("bar", "stacked bar"),
    "bar-horizontal": ("bar", "ranked bar"),
    "area-stacked": ("area", "stacked area"),
}


# ---------------------------------------------------------------------------
# The profile.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldProfile:
    """One field of a result and the role its VALUES play (not its name)."""

    name: str
    role: str  # time | quantitative | categorical | ordinal | grid | hierarchy
    unit: str = ""
    cardinality: int = 0
    count: int = 0
    min: float | None = None
    max: float | None = None

    def to_document(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "role": self.role, "count": self.count}
        if self.unit:
            out["unit"] = self.unit
        if self.role in ("categorical", "ordinal", "hierarchy"):
            out["cardinality"] = self.cardinality
        if self.min is not None:
            out["min"], out["max"] = self.min, self.max
        return out


@dataclass
class Series:
    name: str
    x: list[Any]
    y: list[float | None]
    unit: str = ""
    modelled: bool = False


@dataclass
class Normal:
    """A result in one of four forms every rule is written against.

    ``xy``         series of (x, y) where x is time, a number or a category;
    ``field``      a 2-D grid of values (two categorical or numeric axes);
    ``value``      one value with its reference;
    ``hierarchy``  a tree of named parts with values at the leaves.
    """

    form: str
    x_role: str = ""
    series: list[Series] = field(default_factory=list)
    grid: dict[str, Any] | None = None
    value: dict[str, Any] | None = None
    tree: dict[str, Any] | None = None
    labels: dict[str, list[str]] = field(default_factory=dict)  # state labels per series


@dataclass(frozen=True)
class DataProfile:
    """What a result is, for choosing a picture of it."""

    form: str
    fields: tuple[FieldProfile, ...]
    rows: int
    series_count: int
    units: tuple[str, ...]
    x_role: str = ""
    categories: int = 0
    values_per_series: tuple[int, ...] = ()
    sign: str = "none"  # positive | nonnegative | negative | mixed | none
    range: tuple[float, float] | None = None
    cadence_s: float | None = None
    sparsity: float = 0.0
    reference: Mapping[str, float] = field(default_factory=dict)
    shared_instants: int = 0
    hierarchy_depth: int = 0
    leaves: int = 0
    grid_shape: tuple[int, int] | None = None
    state_labels: int = 0

    def to_document(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "form": self.form,
            "fields": [f.to_document() for f in self.fields],
            "rows": self.rows,
            "series": self.series_count,
            "units": list(self.units),
            "sign": self.sign,
        }
        for key, val in (
            ("x_role", self.x_role), ("categories", self.categories),
            ("cadence_s", self.cadence_s), ("shared_instants", self.shared_instants),
            ("hierarchy_depth", self.hierarchy_depth), ("leaves", self.leaves),
            ("state_labels", self.state_labels),
        ):
            if val:
                out[key] = val
        if self.values_per_series:
            out["values_per_series"] = list(self.values_per_series)
        if self.range is not None:
            out["range"] = list(self.range)
        if self.sparsity:
            out["sparsity"] = round(self.sparsity, 4)
        if self.reference:
            out["reference"] = dict(self.reference)
        if self.grid_shape:
            out["grid"] = list(self.grid_shape)
        return out


class SuggestError(ValueError):
    """A result this module cannot read at all (not one it cannot draw)."""


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _instant(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, str) and _ISO.match(v):
        try:
            m = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return m if m.tzinfo else m.replace(tzinfo=UTC)
    return None


def _iso(v: Any) -> Any:
    m = _instant(v)
    if m is None:
        return v
    return m.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unit(u: Any) -> str:
    return str(u or "").strip()


def _intensive(unit: str) -> bool:
    return unit.casefold() in INTENSIVE_UNITS


def _x_role(xs: Sequence[Any]) -> str:
    present = [x for x in xs if x is not None and x != ""]
    if not present:
        return "index"
    if all(_instant(x) is not None for x in present):
        return "time"
    if all(_num(x) for x in present):
        return "quantitative"
    return "categorical"


# -- reading the inputs ------------------------------------------------------


def _from_series(raw: Sequence[Any]) -> Normal:
    series: list[Series] = []
    for i, s in enumerate(raw):
        if not isinstance(s, Mapping):
            raise SuggestError(f"series[{i}] is not a table")
        name = str(s.get("name") or s.get("channel") or f"series {i + 1}")
        if "points" in s and "y" not in s:
            pts = [p for p in s.get("points") or [] if isinstance(p, Sequence) and len(p) >= 2]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            # Epoch milliseconds, the readout's convention, become instants.
            if xs and all(_num(x) and x > 1e11 for x in xs):
                xs = [datetime.fromtimestamp(x / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ") for x in xs]
        else:
            ys = list(s.get("y") or s.get("values") or [])
            xs = list(s.get("x") or s.get("t") or s.get("ts") or range(len(ys)))
        if len(xs) != len(ys):
            raise SuggestError(f"series {name!r}: {len(xs)} x values for {len(ys)} y values")
        ys2 = [float(v) if _num(v) else None for v in ys]
        series.append(Series(name=name, x=[_iso(x) for x in xs], y=ys2, unit=_unit(s.get("unit")),
                             modelled=bool(s.get("modelled"))))
    names = [s.name for s in series]
    if len(set(names)) != len(names):
        seen: dict[str, int] = {}
        for s in series:
            seen[s.name] = seen.get(s.name, 0) + 1
            if seen[s.name] > 1:
                s.name = f"{s.name} ({seen[s.name]})"
    xs_all = [x for s in series for x in s.x]
    return Normal(form="xy", x_role=_x_role(xs_all), series=series)


def _from_rows(rows: Sequence[Mapping[str, Any]], columns: Sequence[Any] = (),
               units: Mapping[str, str] | None = None) -> Normal:
    """A table, read from its values. Column names are never trusted."""
    rows = [r for r in rows if isinstance(r, Mapping)]
    if not rows:
        raise SuggestError("no rows")
    def _cid(c: Any) -> str:
        return str(c.get("id") if isinstance(c, Mapping) else getattr(c, "id", None) or c)

    def _cunit(c: Any) -> str:
        return str((c.get("unit") if isinstance(c, Mapping) else getattr(c, "unit", "")) or "")

    declared = [_cid(c) for c in columns] or list(rows[0])
    units = dict(units or {})
    for c in columns:
        if _cunit(c):
            units[_cid(c)] = _cunit(c)
    n = len(rows)
    roles: dict[str, str] = {}
    for col in declared:
        vals = [r.get(col) for r in rows if r.get(col) not in (None, "")]
        if len(vals) < max(1, n // 2):
            continue
        if all(_instant(v) is not None for v in vals):
            roles[col] = "time"
        elif all(_num(v) for v in vals):
            roles[col] = "quantitative"
        else:
            roles[col] = "categorical"
    times = [c for c in declared if roles.get(c) == "time"]
    nums = [c for c in declared if roles.get(c) == "quantitative"]
    cats = [c for c in declared if roles.get(c) == "categorical"
            and len({str(r.get(c)) for r in rows}) <= max(CATEGORICAL_MAX, 1)]
    def series_of(xcol: str, ycol: str, group: str | None) -> list[Series]:
        keys: dict[str, Series] = {}
        for r in rows:
            label = ycol if group is None else str(r.get(group))
            name = label if group is None or len(nums) == 1 else f"{label} {ycol}"
            s = keys.setdefault(name, Series(name=name, x=[], y=[], unit=_unit(units.get(ycol))))
            s.x.append(_iso(r.get(xcol)))
            v = r.get(ycol)
            s.y.append(float(v) if _num(v) else None)
        return list(keys.values())

    if times and nums:
        group = cats[0] if cats else None
        series = [s for y in nums for s in series_of(times[0], y, group)]
        return Normal(form="xy", x_role="time", series=series)
    if times and cats and not nums:
        # A label over time: a state. Encoded as ordinal codes, labels kept.
        col = cats[0]
        labels = sorted({str(r.get(col)) for r in rows})
        code = {lab: float(i) for i, lab in enumerate(labels)}
        s = Series(name=col, x=[_iso(r.get(times[0])) for r in rows],
                   y=[code[str(r.get(col))] for r in rows])
        return Normal(form="xy", x_role="time", series=[s], labels={col: labels})
    if len(cats) >= 2 and nums:
        a, b, v = cats[0], cats[1], nums[0]
        xs = list(dict.fromkeys(str(r.get(a)) for r in rows))
        ys = list(dict.fromkeys(str(r.get(b)) for r in rows))
        z: list[list[float | None]] = [[None] * len(xs) for _ in ys]
        for r in rows:
            val = r.get(v)
            if _num(val):
                z[ys.index(str(r.get(b)))][xs.index(str(r.get(a)))] = float(val)
        # The same rows also read as a stacked bar: one series per second category.
        series = [Series(name=yv, x=list(xs), y=list(z[j]), unit=_unit(units.get(v)))
                  for j, yv in enumerate(ys)]
        return Normal(form="field", x_role="categorical", series=series,
                      grid={"x": xs, "y": ys, "z": z, "unit": _unit(units.get(v)), "name": v,
                            "x_name": a, "y_name": b})
    if cats and nums:
        col = cats[0]
        series = [Series(name=y, x=[str(r.get(col)) for r in rows],
                         y=[float(r[y]) if _num(r.get(y)) else None for r in rows],
                         unit=_unit(units.get(y))) for y in nums]
        return Normal(form="xy", x_role="categorical", series=series)
    if len(nums) >= 2:
        xcol = nums[0]
        series = [Series(name=y, x=[r.get(xcol) for r in rows],
                         y=[float(r[y]) if _num(r.get(y)) else None for r in rows],
                         unit=_unit(units.get(y))) for y in nums[1:]]
        norm = Normal(form="xy", x_role="quantitative", series=series)
        norm.labels["_x"] = [xcol, _unit(units.get(xcol))]
        return norm
    if len(nums) == 1:
        col = nums[0]
        vals = [float(r[col]) if _num(r.get(col)) else None for r in rows]
        return Normal(form="xy", x_role="index",
                      series=[Series(name=col, x=list(range(len(vals))), y=vals, unit=_unit(units.get(col)))])
    raise SuggestError("no numeric column and nothing over time: a table of text is a table, not a chart")


def _from_value(raw: Mapping[str, Any]) -> Normal:
    v = raw.get("value")
    if not _num(v):
        raise SuggestError("a value result needs a number in 'value'")
    keep = {k: raw[k] for k in ("value", "unit", "min", "max", "target", "label", "ranges", "trend", "at")
            if k in raw and raw[k] is not None}
    return Normal(form="value", value=keep)


def _tree_stats(node: Mapping[str, Any], depth: int = 1) -> tuple[int, int, list[float]]:
    kids = [c for c in node.get("children") or [] if isinstance(c, Mapping)]
    if not kids:
        v = node.get("value")
        return depth, 1, [float(v)] if _num(v) else []
    d, leaves, vals = depth, 0, []
    for c in kids:
        cd, cl, cv = _tree_stats(c, depth + 1)
        d, leaves, vals = max(d, cd), leaves + cl, vals + cv
    return d, leaves, vals


def _tree_total(node: Mapping[str, Any]) -> float:
    kids = [c for c in node.get("children") or [] if isinstance(c, Mapping)]
    if not kids:
        v = node.get("value")
        return float(v) if _num(v) else 0.0
    return sum(_tree_total(c) for c in kids)


def _from_tree(raw: Mapping[str, Any]) -> Normal:
    if not isinstance(raw.get("children"), Sequence):
        raise SuggestError("a hierarchy is {name, children: [...]} with values at the leaves")
    # The tree also reads as parts (its top level) and as a stack (top by second).
    top = [c for c in raw.get("children") or [] if isinstance(c, Mapping)]
    names = [str(c.get("name") or f"part {i + 1}") for i, c in enumerate(top)]
    unit = _unit(raw.get("unit"))
    parts = Series(name=str(raw.get("name") or "total"), x=names, y=[_tree_total(c) for c in top], unit=unit)
    seconds: dict[str, Series] = {}
    for i, c in enumerate(top):
        for g in c.get("children") or []:
            if not isinstance(g, Mapping):
                continue
            key = str(g.get("name") or "")
            s = seconds.setdefault(key, Series(name=key, x=list(names), y=[None] * len(names), unit=unit))
            s.y[i] = (s.y[i] or 0.0) + _tree_total(g)
    # A stack is read by its biggest parts; past STACK_MAX the rest are one "other".
    ranked = sorted(seconds.values(), key=lambda s: -sum(v or 0.0 for v in s.y))
    if len(ranked) > STACK_MAX:
        keep, rest = ranked[: STACK_MAX - 1], ranked[STACK_MAX - 1:]
        other = Series(name="other", x=list(names), unit=unit,
                       y=[sum(s.y[i] or 0.0 for s in rest) or None for i in range(len(names))])
        ranked = [*keep, other]
    return Normal(form="hierarchy", x_role="categorical", series=[parts], tree=dict(raw),
                  labels={"_stack": [s.name for s in ranked]}, grid={"stack": [s.__dict__ for s in ranked]})


def normalise(result: Any, *, columns: Sequence[Any] = (), units: Mapping[str, str] | None = None) -> Normal:
    """Read a result in any of the shapes the data door and chat hand around.

    Accepted: ``{"series": [...]}`` (inline series, or readout series with
    ``points``), a list of row tables or ``{"rows": [...], "columns": [...]}``,
    ``{"value": n, ...}``, ``{"children": [...]}`` (a hierarchy), or
    ``{"field": {x, y, z}}``. A data-door envelope (``{"value": {...}}``
    wrapping rows or series) is unwrapped once.
    """
    if isinstance(result, Mapping) and isinstance(result.get("value"), Mapping):
        result = result["value"]
    if isinstance(result, Mapping) and isinstance(result.get("readout"), Mapping):
        result = result["readout"]
    if isinstance(result, Mapping):
        if isinstance(result.get("field"), Mapping):
            f = dict(result["field"])
            z = f.get("z") or []
            xs = list(f.get("x") or range(len(z[0]) if z else 0))
            ys = list(f.get("y") or range(len(z)))
            series = [Series(name=str(ys[j]), x=list(xs), y=[float(v) if _num(v) else None for v in row],
                             unit=_unit(f.get("unit"))) for j, row in enumerate(z)]
            return Normal(form="field", x_role=_x_role(xs), series=series,
                          grid={"x": xs, "y": ys, "z": z, "unit": _unit(f.get("unit")), "name": f.get("name") or ""})
        if isinstance(result.get("series"), Sequence) and not isinstance(result.get("series"), str):
            return _from_series(result["series"])
        if isinstance(result.get("rows"), Sequence):
            return _from_rows(result["rows"], result.get("columns") or columns,
                              {**(result.get("units") or {}), **(units or {})})
        if "children" in result:
            return _from_tree(result)
        if "value" in result:
            return _from_value(result)
    if isinstance(result, Sequence) and not isinstance(result, (str, bytes)):
        items = list(result)
        if items and all(isinstance(i, Mapping) and ("y" in i or "points" in i) for i in items):
            return _from_series(items)
        return _from_rows(items, columns, units)
    raise SuggestError("not a result this can read: series, rows, a value or a hierarchy")


# -- profiling ----------------------------------------------------------------


def _reference(norm: Normal, units: tuple[str, ...], hint: Mapping[str, Any] | None) -> dict[str, float]:
    ref: dict[str, float] = {}
    src = dict(norm.value or {})
    src.update({k: v for k, v in (hint or {}).items() if v is not None})
    for k in ("min", "max", "target"):
        if _num(src.get(k)):
            ref[k] = float(src[k])
    # A percentage is a share of 100: the reference is in the unit itself.
    if "max" not in ref and "target" not in ref and units and all(u.casefold() in PERCENT_UNITS for u in units):
        ref["target"] = 100.0
        ref.setdefault("min", 0.0)
    return ref


def profile(norm: Normal, *, reference: Mapping[str, Any] | None = None) -> DataProfile:
    """The profile of a normalised result. Pure and deterministic."""
    if norm.form == "value":
        v = float(norm.value["value"])  # type: ignore[index]
        unit = _unit((norm.value or {}).get("unit"))
        units = (unit,) if unit else ()
        f = FieldProfile(name=str((norm.value or {}).get("label") or "value"), role="quantitative",
                         unit=unit, count=1, min=v, max=v)
        return DataProfile(form="value", fields=(f,), rows=1, series_count=1, units=units,
                           sign=_sign([v]), range=(v, v), reference=_reference(norm, units, reference),
                           values_per_series=(1,))
    series = norm.series
    units = tuple(dict.fromkeys(s.unit for s in series if s.unit))
    vals = [v for s in series for v in s.y if v is not None]
    total = sum(len(s.y) for s in series)
    fields: list[FieldProfile] = []
    x_all = [x for s in series for x in s.x]
    distinct_x = list(dict.fromkeys(str(x) for x in x_all))
    cadence = None
    shared = 0
    if norm.x_role == "time":
        instants = sorted({m for x in x_all if (m := _instant(x)) is not None})
        fields.append(FieldProfile(name="time", role="time", count=len(instants), cardinality=len(instants)))
        if len(instants) >= 2:
            gaps = sorted((b - a).total_seconds() for a, b in zip(instants, instants[1:], strict=False))
            cadence = gaps[len(gaps) // 2]
        if len(series) >= 2:
            common = set(series[0].x)
            for s in series[1:]:
                common &= {x for x, y in zip(s.x, s.y, strict=False) if y is not None}
            shared = len(common)
    elif norm.x_role == "quantitative":
        xs = [float(x) for x in x_all if _num(x)]
        xname = (norm.labels.get("_x") or ["x", ""])
        fields.append(FieldProfile(name=xname[0], role="quantitative", unit=xname[1], count=len(xs),
                                   min=min(xs) if xs else None, max=max(xs) if xs else None))
        shared = len(xs)
    elif norm.x_role == "categorical":
        role = "hierarchy" if norm.form == "hierarchy" else "categorical"
        fields.append(FieldProfile(name="category", role=role, cardinality=len(distinct_x), count=len(x_all)))
    for s in series:
        ys = [v for v in s.y if v is not None]
        fields.append(FieldProfile(name=s.name, role="ordinal" if s.name in norm.labels else "quantitative",
                                   unit=s.unit, count=len(ys), min=min(ys) if ys else None,
                                   max=max(ys) if ys else None,
                                   cardinality=len(norm.labels.get(s.name, []))))
    if norm.form == "field" and norm.grid:
        fields.append(FieldProfile(name=str(norm.grid.get("name") or "value"), role="grid",
                                   unit=_unit(norm.grid.get("unit")), count=len(vals)))
    depth = leaves = 0
    if norm.tree is not None:
        depth, leaves, _ = _tree_stats(norm.tree)
        depth -= 1
    return DataProfile(
        form=norm.form,
        fields=tuple(fields),
        rows=max((len(s.y) for s in series), default=0),
        series_count=len(series),
        units=units,
        x_role=norm.x_role,
        categories=len(distinct_x) if norm.x_role in ("categorical",) else 0,
        values_per_series=tuple(sum(1 for v in s.y if v is not None) for s in series),
        sign=_sign(vals),
        range=(min(vals), max(vals)) if vals else None,
        cadence_s=cadence,
        sparsity=(1 - len(vals) / total) if total else 0.0,
        reference=_reference(norm, units, reference),
        shared_instants=shared,
        hierarchy_depth=depth,
        leaves=leaves,
        grid_shape=(len(norm.grid["y"]), len(norm.grid["x"])) if norm.form == "field" and norm.grid
        and "z" in norm.grid else None,
        state_labels=max((len(v) for k, v in norm.labels.items() if not k.startswith("_")), default=0),
    )


def _sign(vals: Sequence[float]) -> str:
    if not vals:
        return "none"
    lo, hi = min(vals), max(vals)
    if lo > 0:
        return "positive"
    if lo >= 0:
        return "nonnegative"
    if hi < 0:
        return "negative"
    return "mixed"


# ---------------------------------------------------------------------------
# Compatibility: the rules. Each returns "" when the kind can draw the
# profile honestly, or the reason it cannot, in a reader's words.
# ---------------------------------------------------------------------------


def _has_reference(p: DataProfile) -> bool:
    return "max" in p.reference or "target" in p.reference


def _one_unit(p: DataProfile) -> str:
    return "" if len(p.units) <= 1 else f"its series are in {len(p.units)} units ({', '.join(p.units)}), and one axis would mix them"


def _rule_line(p: DataProfile) -> str:
    if p.form not in ("xy",) or p.x_role not in ("time", "quantitative"):
        return "a line joins values in order, and these have no time or numeric axis to order them by"
    if max(p.values_per_series, default=0) < LINE_MIN_POINTS:
        return "a line needs at least two points"
    if p.state_labels:
        return "the values are labels, not amounts; a line between labels invents the values in between"
    return _one_unit(p)


def _rule_line_dual(p: DataProfile) -> str:
    base = _rule_line(p)
    if base and not base.startswith("its series are in"):
        return base
    if len(p.units) != 2:
        return ("a second axis is for exactly two units; these are in "
                f"{len(p.units) or 'no'} unit{'s' if len(p.units) != 1 else ''}")
    return ""


def _rule_line_panels(p: DataProfile) -> str:
    base = _rule_line(p)
    if base and not base.startswith("its series are in"):
        return base
    if len(p.units) < 2:
        return "one panel per unit needs two units or more"
    return ""


def _rule_area(p: DataProfile) -> str:
    base = _rule_line(p)
    if base:
        return base
    if p.sign in ("negative", "mixed"):
        return "an area is filled to zero; negative values would fill the wrong way"
    if any(_intensive(u) for u in p.units):
        return f"an area reads as an amount, and {p.units[0]} is an intensity, not an amount"
    return ""


def _rule_area_stacked(p: DataProfile) -> str:
    base = _rule_area(p)
    if base:
        return base
    if p.series_count < 2:
        return "a stack needs two series or more"
    if p.x_role != "time":
        return "a stacked area is composition over time, and there is no time axis"
    return ""


def _rule_step(p: DataProfile) -> str:
    if p.x_role != "time":
        return "a step holds a value until the next reading, which needs a time axis"
    return _rule_line(p) if not p.state_labels else ""


def _rule_sparkline(p: DataProfile) -> str:
    # Each sparkline is its own row with its own scale, so units may differ.
    base = _rule_line(p)
    if base and not base.startswith("its series are in"):
        return base
    if p.x_role != "time":
        return "a sparkline is a recent shape over time"
    if p.series_count > KPI_MAX_SERIES:
        return f"more than {KPI_MAX_SERIES} sparklines is a table"
    return ""


def _rule_bar(p: DataProfile) -> str:
    if p.form == "value":
        return "one value is a number, not a bar chart"
    if p.x_role == "categorical":
        if p.categories > BAR_MAX_CATEGORIES:
            return f"{p.categories} categories; bars stop being legible past {BAR_MAX_CATEGORIES}"
        return _one_unit(p)
    if p.x_role == "time":
        if p.rows > BAR_MAX_CATEGORIES:
            return (f"{p.rows} instants; a bar per instant stops being legible past "
                    f"{BAR_MAX_CATEGORIES} (a line draws this)")
        return _one_unit(p)
    return "bars are one per category, and nothing here is a category"


def _rule_bar_stacked(p: DataProfile) -> str:
    if p.form == "hierarchy":
        if p.hierarchy_depth < 2:
            return "a stacked bar of a hierarchy needs two levels"
        return ""
    base = _rule_bar(p)
    if base:
        return base
    if p.series_count < 2:
        return "a stack needs two series or more"
    if p.sign in ("negative", "mixed"):
        return "stacked bars of negative values do not add up to a whole"
    if any(_intensive(u) for u in p.units):
        return f"stacking adds values, and {p.units[0]} does not add"
    return ""


def _rule_bar_horizontal(p: DataProfile) -> str:
    if p.x_role != "categorical" or p.form not in ("xy", "hierarchy"):
        return "a ranked bar orders categories, and nothing here is a category"
    if p.series_count != 1:
        return "a ranking orders one value per category"
    return _rule_bar(p) if p.form == "xy" else ""


def _rule_pie(p: DataProfile) -> str:
    if p.x_role == "time":
        return "a time axis is not parts of a whole; a pie would hide the order"
    if p.x_role != "categorical":
        return "a pie divides a whole into named parts, and there are no named parts"
    if p.series_count != 1:
        return "a pie draws one whole; these are several series"
    if p.categories > PIE_MAX_SLICES:
        return f"{p.categories} parts; slices stop being comparable past {PIE_MAX_SLICES}"
    if p.categories < 2:
        return "one part is the whole; a pie of one slice says nothing"
    if p.sign in ("negative", "mixed"):
        return "a slice cannot be negative, and some values are"
    if p.sign == "none":
        return "no values"
    if p.units and _intensive(p.units[0]):
        return f"{p.units[0]} is an intensity; parts of a whole must add up"
    return ""


def _rule_histogram(p: DataProfile) -> str:
    if p.form not in ("xy", "field"):
        return "a histogram bins many values of one quantity"
    if p.state_labels:
        return "the values are labels, not amounts"
    n = max(p.values_per_series, default=0)
    if n < HISTOGRAM_MIN_VALUES:
        return f"{n} values; a histogram needs at least {HISTOGRAM_MIN_VALUES} to show a shape"
    if p.x_role == "categorical" and p.form == "xy":
        return "one value per category is a bar chart, not a distribution"
    return _one_unit(p)


def _rule_box(p: DataProfile) -> str:
    if p.form not in ("xy",):
        return "a box summarises many values per group"
    if p.state_labels:
        return "the values are labels, not amounts"
    if p.x_role == "categorical":
        return "one value per category has no spread to summarise"
    if p.series_count > BOX_MAX_GROUPS:
        return f"{p.series_count} groups; boxes stop being comparable past {BOX_MAX_GROUPS}"
    n = min(p.values_per_series, default=0)
    if n < BOX_MIN_VALUES:
        return f"a group has {n} values; a box needs {BOX_MIN_VALUES} to have quartiles"
    return _one_unit(p)


def _rule_scatter(p: DataProfile) -> str:
    if p.form != "xy" or p.state_labels:
        return "a scatter plots one quantity against another"
    if p.x_role == "quantitative":
        if p.shared_instants < SCATTER_MIN_PAIRS:
            return f"{p.shared_instants} pairs; a relationship needs at least {SCATTER_MIN_PAIRS}"
        return ""
    if p.x_role == "time":
        if p.series_count != 2:
            return ("a scatter pairs exactly two quantities read at the same instants; "
                    f"these are {p.series_count} series")
        if p.shared_instants < SCATTER_MIN_PAIRS:
            return (f"the two series share {p.shared_instants} instants; a relationship needs "
                    f"at least {SCATTER_MIN_PAIRS}")
        return ""
    return "a scatter needs two quantities, and one of these is a category"


def _rule_heatmap(p: DataProfile) -> str:
    if p.form == "field":
        return ""
    if p.form == "xy" and p.x_role == "time" and p.series_count >= HEATMAP_MIN_ROWS:
        return _one_unit(p) or ""
    return "a heatmap needs a 2-D grid: two categorical axes, or time by three series or more of one unit"


def _value_rule(name: str, *, needs_max: bool = False) -> Any:
    def rule(p: DataProfile) -> str:
        if p.form == "xy":
            if p.x_role == "categorical":
                return f"a {name} shows one value against its limit; these are many categories"
            if p.series_count != 1:
                return f"a {name} shows one value; these are {p.series_count} series"
        elif p.form != "value":
            return f"a {name} shows one value against its limit"
        if needs_max and "max" not in p.reference and "target" not in p.reference:
            return f"a {name} needs the value's maximum or limit, and none is known"
        if not _has_reference(p):
            return f"a {name} needs a limit or a target to measure the value against, and none is known"
        return ""
    return rule


def _rule_kpi(p: DataProfile) -> str:
    if p.form == "value":
        # A bare number is the sentence the answer already says; a tile adds
        # nothing unless it has something to be read against.
        return "" if _has_reference(p) else "one number with nothing to compare it to is the sentence, not a chart"
    if p.form != "xy" or p.x_role != "time":
        return "a KPI tile is a current value over time, and there is no time axis"
    if p.state_labels:
        return "the values are labels, not amounts"
    if p.series_count > KPI_MAX_SERIES:
        return f"more than {KPI_MAX_SERIES} tiles is a table"
    return ""


def _rule_state(p: DataProfile) -> str:
    if p.x_role != "time":
        return "a state is a label over time, and there is no time axis"
    if not p.state_labels:
        return "a state draws labels held over time; these values are amounts"
    if p.state_labels > STATE_MAX_LABELS:
        return f"{p.state_labels} labels; a state has at most {STATE_MAX_LABELS}"
    return ""


def _rule_flame(p: DataProfile) -> str:
    if p.form != "hierarchy":
        return "a flame graph draws a hierarchy, and these values have no parents"
    if p.hierarchy_depth < 1:
        return "a hierarchy of one level is a bar chart"
    if p.sign in ("negative", "mixed"):
        return "a part's width cannot be negative"
    return ""


def _rule_scene(p: DataProfile) -> str:
    # A 3D kind draws a ``data.scene`` (a mesh, a grid, a point cloud), which
    # a data answer never is: it is opened from a model or an imported figure.
    return "a 3D scene draws a mesh, grid or point cloud, and an answer's values are not one"


#: The compatibility table. Each row is a chip the switcher can show: a kind or
#: a variant of one, the rule that admits it, and the rule's summary as written
#: in ADR-215. Order here is the fixed tie-break order.
RULES: dict[str, tuple[Any, str]] = {
    "line": (_rule_line, "time or numeric x; ≥2 points; one unit; amounts, not labels"),
    "line-dual": (_rule_line_dual, "as line, with exactly two units"),
    "line-panels": (_rule_line_panels, "as line, with two units or more"),
    "bar": (_rule_bar, "categories ≤40 (or ≤40 instants); one unit"),
    "bar-horizontal": (_rule_bar_horizontal, "one value per category ≤40"),
    "bar-stacked": (_rule_bar_stacked, "≥2 series of an additive, non-negative unit; or a 2-level hierarchy"),
    "scatter": (_rule_scatter, "two quantities paired: numeric x, or two series sharing ≥5 instants"),
    "area": (_rule_area, "as line; non-negative; an amount, not an intensity"),
    "area-stacked": (_rule_area_stacked, "as area; ≥2 series over time"),
    "histogram": (_rule_histogram, "≥20 values of one unit"),
    "box": (_rule_box, "≥5 values per group, ≤24 groups, one unit"),
    "step": (_rule_step, "time x"),
    "pie": (_rule_pie, "one series of 2–8 named parts, all > 0, additive unit, no time axis"),
    "donut": (_rule_pie, "as pie"),
    "heatmap": (_rule_heatmap, "a 2-D grid, or time × ≥3 series of one unit"),
    "gauge": (_value_rule("gauge", needs_max=True), "one value with a limit or target"),
    "bullet": (_value_rule("bullet"), "one value with a target or limit"),
    "progress": (_value_rule("progress bar", needs_max=True), "one value with a maximum"),
    "kpi": (_rule_kpi, "≤6 series over time; a lone value only with a reference"),
    "sparkline": (_rule_sparkline, "as line over time; ≤6 series"),
    "state": (_rule_state, "a label (≤12 distinct) over time"),
    "flame": (_rule_flame, "a hierarchy of ≥2 levels, non-negative"),
    "mesh3d": (_rule_scene, "never from an answer; a scene"),
    "surface": (_rule_scene, "never from an answer; a scene"),
    "scatter3d": (_rule_scene, "never from an answer; a scene"),
    "volume": (_rule_scene, "never from an answer; a scene"),
}


def chip_kind(chip: str) -> str:
    """The ChartSpec kind a chip draws (a variant draws its base kind)."""
    return VARIANTS.get(chip, (chip, ""))[0]


def chip_label(chip: str) -> str:
    return VARIANTS.get(chip, (chip, chip))[1] or chip


def registered_chips() -> tuple[str, ...]:
    """The chips whose kind the renderer has registered. A rule for a kind
    nothing draws is a promise; it is withheld until the kind lands."""
    kinds = set(us.registered_kinds())
    return tuple(c for c in RULES if chip_kind(c) in kinds)


@dataclass(frozen=True)
class Verdict:
    chip: str
    kind: str
    valid: bool
    reason: str

    def to_document(self) -> dict[str, Any]:
        return {"chip": self.chip, "kind": self.kind, "valid": self.valid, "reason": self.reason}


def compatible(p: DataProfile, *, chips: Sequence[str] | None = None) -> list[Verdict]:
    """Every chip, valid or not, with why. Deterministic; never consults a model."""
    out = []
    for chip in chips if chips is not None else registered_chips():
        rule = RULES.get(chip)
        if rule is None:
            out.append(Verdict(chip, chip_kind(chip), False, f"no rule for {chip!r}"))
            continue
        why = rule[0](p)
        out.append(Verdict(chip, chip_kind(chip), not why, why or _admits(chip, p)))
    return out


def _admits(chip: str, p: DataProfile) -> str:
    """A one-line reason a valid chip fits, in the data's own terms."""
    n = p.series_count
    unit = p.units[0] if p.units else ""
    many = f"{n} series" if n > 1 else "the readings"
    return {
        "line": f"{many} over {'time' if p.x_role == 'time' else 'a numeric axis'}",
        "line-dual": f"{' and '.join(p.units)} on their own axes, over time",
        "line-panels": f"one panel per unit ({', '.join(p.units)})",
        "bar": f"compares {p.categories or p.rows} {'categories' if p.x_role == 'categorical' else 'instants'}",
        "bar-horizontal": f"ranks {p.categories} categories by value",
        "bar-stacked": "each part's share stacked within its whole",
        "scatter": "one quantity against the other, reading by reading",
        "area": f"{many} filled to zero, as an amount",
        "area-stacked": "parts stacked into their total over time",
        "histogram": f"how {max(p.values_per_series, default=0)} values of {unit or 'one quantity'} are distributed",
        "box": f"median, quartiles and range of {'each of ' + str(n) + ' series' if n > 1 else 'the values'}",
        "step": "each reading held until the next",
        "pie": f"{p.categories} parts of one whole",
        "donut": f"{p.categories} parts of one whole, total in the middle",
        "heatmap": "a value over two axes, as colour",
        "gauge": _ref_phrase("against", p),
        "bullet": _ref_phrase("against", p),
        "progress": _ref_phrase("toward", p),
        "kpi": "the current value" + ("s" if n > 1 else "") + " and recent trend",
        "sparkline": "the recent shape at a glance",
        "state": "labels as spans over time",
        "flame": f"a {p.hierarchy_depth + 1}-level breakdown, widths as shares",
    }.get(chip, "fits")


def _ref_phrase(prep: str, p: DataProfile) -> str:
    ref = p.reference
    unit = p.units[0] if p.units else ""
    if "target" in ref:
        return f"the value {prep} its reference of {_fmt(ref['target'])}{_u(unit)}"
    if "max" in ref:
        return f"the value {prep} its limit of {_fmt(ref['max'])}{_u(unit)}"
    return "the value"


def _fmt(v: float) -> str:
    return f"{v:g}"


def _u(unit: str) -> str:
    if not unit:
        return ""
    return "%" if unit.casefold() in PERCENT_UNITS else f" {unit}"


# ---------------------------------------------------------------------------
# Intents and ranking.
# ---------------------------------------------------------------------------

INTENTS: tuple[str, ...] = (
    "trend", "compare", "part_of_whole", "distribution", "relationship", "status",
    "ranking", "hierarchy", "composition_over_time",
)

_CUES: dict[str, tuple[str, ...]] = {
    "trend": ("over time", "trend", "behave", "behaved", "history", "since", "last week", "last month",
              "yesterday", "today", "how did", "change", "changed", "evolve", "timeline", "during", "when"),
    "compare": ("compare", "comparison", " vs", "versus", "against", "between", "difference", "differ",
                " and "),
    "part_of_whole": ("share", "proportion", "percent of", "fraction", "split", "make up", "makes up",
                      "composition", "portion", "breakdown of the total", "of the total"),
    "distribution": ("distribution", "distributed", "spread", "range", "typical", "variation", "vary",
                     "histogram", "how often", "outlier", "behave", "behaved", "steady", "stable"),
    "relationship": ("correlat", "relationship", "relate", "depend", "affect", "track", "follow",
                     "respond", "as a function of"),
    "status": ("close to", "how close", "limit", "rated", "target", "threshold", "capacity", "how near",
               "headroom", "setpoint", "maximum allowed", "budget", "goal", "quota", "reach"),
    "ranking": ("most", "least", "top", "bottom", "rank", "highest", "lowest", "largest", "smallest",
                "biggest", "which", "best", "worst"),
    "hierarchy": ("breakdown", "break down", "drill", "hierarch", "nested", "where does", "where do",
                  "made of", "composed of", "tree", "go to", "goes"),
    "composition_over_time": ("mix over time", "share over time", "composition over", "stacked over"),
}

#: Which chips each intent prefers, best first. A chip absent from an intent's
#: list is not refused by it, only unranked by it.
PREFERENCES: dict[str, tuple[str, ...]] = {
    "trend": ("line", "line-dual", "line-panels", "area", "step", "sparkline", "kpi", "bar"),
    "compare": ("line-dual", "line-panels", "scatter", "bar", "box", "line", "bar-stacked"),
    "part_of_whole": ("pie", "donut", "bar-stacked", "area-stacked", "flame", "bar"),
    "distribution": ("box", "histogram", "line"),
    "relationship": ("scatter", "line-dual", "heatmap"),
    "status": ("gauge", "bullet", "progress", "kpi"),
    "ranking": ("bar-horizontal", "bar", "pie"),
    "hierarchy": ("flame", "bar-stacked", "pie", "bar"),
    "composition_over_time": ("area-stacked", "bar-stacked", "line"),
}

#: What a result is drawn as when the question names no intent: by its form.
DEFAULT_INTENTS: dict[str, tuple[str, ...]] = {
    "xy:time": ("trend",),
    "xy:categorical": ("ranking", "part_of_whole"),
    "xy:quantitative": ("relationship",),
    "xy:index": ("distribution",),
    "value": ("status",),
    "field": ("relationship",),
    "hierarchy": ("hierarchy",),
}


def intents(question: str, p: DataProfile | None = None) -> tuple[str, ...]:
    """The intents a question carries, strongest first. Deterministic.

    Cues are counted; ties break in :data:`INTENTS` order. A question with no
    cue takes the data's default intent, so "show me this" still ranks.
    """
    q = f" {(question or '').casefold()} "
    scores: dict[str, int] = {}
    for name in INTENTS:
        hits = sum(1 for cue in _CUES[name] if cue in q)
        if hits:
            scores[name] = hits
    found = sorted(scores, key=lambda n: (-scores[n], INTENTS.index(n)))
    if p is not None:
        key = p.form if p.form != "xy" else f"xy:{p.x_role}"
        defaults = DEFAULT_INTENTS.get(key, ())
        # A ranking cue ("which") over data with no categories is not a ranking.
        if p.x_role != "categorical":
            found = [n for n in found if n not in ("ranking",)]
        if p.form != "value" and not _has_reference(p):
            found = [n for n in found if n != "status"]
        for d in defaults:
            if d not in found:
                found.append(d)
    return tuple(found)


def rank(valid: Sequence[str], wanted: Sequence[str], p: DataProfile | None = None,
         *, prefer: Sequence[str] = ()) -> list[str]:
    """The valid chips, best first.

    Each intent contributes a score to the chips it prefers, the first intent
    weighing most. Profile-aware nudges then apply (a single series wants a
    histogram before a box; many series a box before a histogram). ``prefer``,
    a model's or person's choice, moves chips to the front, and only chips in
    ``valid``: an invalid chip in ``prefer`` is ignored, never admitted.
    """
    valid = list(valid)
    score: dict[str, float] = {c: 0.0 for c in valid}
    for i, intent in enumerate(wanted):
        weight = 1.0 / (i + 1)
        prefs = list(PREFERENCES.get(intent, ()))
        if intent == "distribution" and p is not None and p.series_count == 1:
            prefs = ["histogram", "box", *[c for c in prefs if c not in ("histogram", "box")]]
        for j, chip in enumerate(prefs):
            if chip in score:
                score[chip] += weight * (len(prefs) - j) / len(prefs)
    order = {c: i for i, c in enumerate(RULES)}
    ranked = sorted(valid, key=lambda c: (-score[c], order.get(c, len(order))))
    front = [c for c in dict.fromkeys(prefer) if c in score]
    return front + [c for c in ranked if c not in front]


#: Chips that are one picture drawn another way. The top row offers different
#: pictures, so it takes one of each family before it takes a second.
FAMILIES: dict[str, str] = {
    "line": "line", "line-dual": "line", "line-panels": "line", "area": "line", "area-stacked": "line",
    "step": "line", "sparkline": "line", "bar": "bar", "bar-horizontal": "bar", "bar-stacked": "bar",
    "pie": "pie", "donut": "pie",
}

#: How deep into an intent's preferences its own pick may reach.
INTENT_REACH = 3


def family(chip: str) -> str:
    return FAMILIES.get(chip, chip)


def pick_head(ordered: Sequence[str], wanted: Sequence[str], *, top: int = TOP,
              prefer: Sequence[str] = ()) -> list[str]:
    """The top row: different pictures, one per intent first.

    ``prefer`` goes first, as asked. Then each of the first two intents
    contributes its best valid chip from its first :data:`INTENT_REACH`
    preferences, a family at a time; then the overall ranking fills the row,
    still a family at a time. A row of two different pictures is better than a
    third that is the first drawn again; those wait under "more".
    """
    valid = list(ordered)
    head: list[str] = [c for c in dict.fromkeys(prefer) if c in valid][:top]
    for intent in list(wanted)[:2]:
        if len(head) >= top:
            break
        for chip in PREFERENCES.get(intent, ())[:INTENT_REACH]:
            if chip in valid and chip not in head and family(chip) not in {family(h) for h in head}:
                head.append(chip)
                break
    for chip in valid:
        if len(head) >= top:
            break
        if chip not in head and family(chip) not in {family(h) for h in head}:
            head.append(chip)
    if not head and valid:
        head.append(valid[0])
    # The row reads in ranked order, except that what was asked for leads.
    asked = [c for c in head if c in prefer]
    return asked + [c for c in valid if c in head and c not in asked]


# ---------------------------------------------------------------------------
# Binding: a ChartSpec 2.0 per chip.
# ---------------------------------------------------------------------------


def _decimate(s: Series, budget: int) -> Series:
    """Keep each bucket's first, min and max so peaks survive decimation."""
    n = len(s.y)
    if n <= budget or budget < 4:
        return s
    buckets = max(1, budget // 3)
    size = n / buckets
    keep: list[int] = []
    for b in range(buckets):
        lo, hi = int(b * size), min(n, int((b + 1) * size))
        if lo >= hi:
            continue
        idx = [i for i in range(lo, hi) if s.y[i] is not None]
        pick = {lo}
        if idx:
            pick.add(min(idx, key=lambda i: s.y[i]))  # type: ignore[arg-type,return-value]
            pick.add(max(idx, key=lambda i: s.y[i]))  # type: ignore[arg-type,return-value]
        keep.extend(sorted(pick))
    keep.append(n - 1)
    keep = sorted(set(keep))
    return Series(name=s.name, x=[s.x[i] for i in keep], y=[s.y[i] for i in keep], unit=s.unit,
                  modelled=s.modelled)


def _stride(s: Series, budget: int) -> Series:
    """An even sample, for kinds that count values (a histogram, a box)."""
    vals = [(x, y) for x, y in zip(s.x, s.y, strict=False) if y is not None]
    if len(vals) <= budget:
        return Series(name=s.name, x=[x for x, _ in vals], y=[y for _, y in vals], unit=s.unit)
    step = len(vals) / budget
    pick = [vals[int(i * step)] for i in range(budget)]
    return Series(name=s.name, x=[x for x, _ in pick], y=[y for _, y in pick], unit=s.unit)


def _series_doc(s: Series, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"name": s.name, "x": list(s.x), "y": list(s.y)}
    if s.unit:
        out["unit"] = s.unit
    if s.modelled:
        out["modelled"] = True
    out.update(extra)
    return out


def _last(s: Series) -> float | None:
    for v in reversed(s.y):
        if v is not None:
            return v
    return None


def _value_block(norm: Normal, p: DataProfile) -> dict[str, Any]:
    if norm.form == "value":
        v = dict(norm.value or {})
    else:
        s = norm.series[0]
        ys = [y for y in s.y if y is not None]
        v = {"value": max(ys) if ys else None, "unit": s.unit, "label": s.name,
             "trend": [y for y in s.y][-60:]}
    v.update({k: val for k, val in p.reference.items() if k not in v})
    value = v.get("value")
    if "max" not in v and _num(value):
        target = v.get("target")
        top = max(float(value), float(target) if _num(target) else float(value))
        v["max"] = float(f"{top * 1.15:.3g}") if top > 0 else 1.0
    if "min" not in v:
        v["min"] = 0.0
    if _num(v.get("min")) and _num(v.get("max")) and v["min"] >= v["max"]:
        v["max"] = v["min"] + 1.0
    return {k: val for k, val in v.items() if k in ("value", "unit", "min", "max", "target", "label",
                                                     "ranges", "trend", "at") and val is not None}


def _bind(chip: str, norm: Normal, p: DataProfile, *, title: str, budget: int) -> dict[str, Any]:
    kind = chip_kind(chip)
    doc: dict[str, Any] = {"schema_version": us.SCHEMA_VERSION, "kind": kind, "title": title}
    series = norm.series
    if kind in ("line", "area", "step", "sparkline", "kpi", "state") and norm.form != "value":
        dec = [_decimate(s, budget) for s in series]
        if chip == "line-dual":
            first = p.units[0]
            doc["data"] = {"series": [_series_doc(s, axis="y" if s.unit == first else "y2") for s in dec]}
            doc["axes"] = {"x": {"type": "time"} if p.x_role == "time" else {"type": "linear"},
                           "y": {"unit": p.units[0]}, "y2": {"unit": p.units[1]}}
        elif chip == "line-panels":
            panels = [{"id": f"p{i + 1}", "title": u} for i, u in enumerate(p.units)]
            pid = {u: f"p{i + 1}" for i, u in enumerate(p.units)}
            doc["data"] = {"series": [_series_doc(s, panel=pid.get(s.unit, "p1")) for s in dec]}
            doc["layout"] = {"panels": panels, "columns": 1}
        else:
            doc["data"] = {"series": [_series_doc(s) for s in dec]}
        if chip == "area-stacked":
            doc["encoding"] = {"stack": "stacked"}
        if kind in ("line", "area", "step", "state") and chip not in ("line-dual", "line-panels"):
            doc["axes"] = {"x": {"type": "time" if p.x_role == "time" else "linear"}}
            if p.units:
                doc["axes"]["y"] = {"unit": p.units[0]}
            if norm.labels.get("_x"):
                doc["axes"]["x"].update({"title": norm.labels["_x"][0], **({"unit": norm.labels["_x"][1]}
                                                                          if norm.labels["_x"][1] else {})})
    elif kind in ("bar", "pie", "donut"):
        if chip == "bar-stacked" and norm.form == "hierarchy":
            stack = (norm.grid or {}).get("stack") or []
            doc["data"] = {"series": [{"name": s["name"], "x": s["x"],
                                       "y": [v if v is not None else 0.0 for v in s["y"]],
                                       **({"unit": s["unit"]} if s.get("unit") else {})} for s in stack]}
        else:
            src = series if p.x_role == "categorical" else [_decimate(s, BAR_MAX_CATEGORIES) for s in series]
            doc["data"] = {"series": [_series_doc(s) for s in src]}
        if chip == "bar-stacked":
            doc["encoding"] = {"stack": "stacked"}
        elif chip == "bar-horizontal":
            s = src[0]
            pairs = sorted(zip(s.x, s.y, strict=False), key=lambda xy: -(xy[1] if xy[1] is not None else -math.inf))
            doc["data"] = {"series": [_series_doc(Series(s.name, [x for x, _ in pairs], [y for _, y in pairs],
                                                         s.unit))]}
            doc["encoding"] = {"orientation": "horizontal"}
        if kind == "bar" and p.x_role == "categorical":
            doc["axes"] = {"x": {"type": "category"}, **({"y": {"unit": p.units[0]}} if p.units else {})}
    elif kind in ("histogram", "box"):
        doc["data"] = {"series": [_series_doc(_stride(s, max(budget, 200))) for s in series]}
        if p.units:
            doc["axes"] = {"y": {"unit": p.units[0]}} if kind == "box" else {"x": {"unit": p.units[0]}}
    elif kind == "scatter":
        if p.x_role == "time":
            a, b = series[0], series[1]
            bmap = {x: y for x, y in zip(b.x, b.y, strict=False) if y is not None}
            pairs = [(y, bmap[x]) for x, y in zip(a.x, a.y, strict=False) if y is not None and x in bmap]
            if len(pairs) > budget:
                step = len(pairs) / budget
                pairs = [pairs[int(i * step)] for i in range(budget)]
            doc["data"] = {"series": [{"name": f"{b.name} against {a.name}", "x": [x for x, _ in pairs],
                                       "y": [y for _, y in pairs], **({"unit": b.unit} if b.unit else {})}]}
            doc["axes"] = {"x": {"type": "linear", "title": a.name, **({"unit": a.unit} if a.unit else {})},
                           "y": {"title": b.name, **({"unit": b.unit} if b.unit else {})}}
        else:
            doc["data"] = {"series": [_series_doc(_stride(s, budget)) for s in series]}
            xl = norm.labels.get("_x") or ["", ""]
            doc["axes"] = {"x": {"type": "linear", **({"title": xl[0]} if xl[0] else {}),
                                 **({"unit": xl[1]} if xl[1] else {})}}
    elif kind == "heatmap":
        if norm.form == "field" and norm.grid and "z" in norm.grid:
            g = norm.grid
            doc["data"] = {"field": {"x": [str(x) for x in g["x"]], "y": [str(y) for y in g["y"]],
                                     "z": g["z"], **({"unit": g["unit"]} if g.get("unit") else {})}}
        else:
            # time by series: bucket every series onto one shared set of instants.
            cols = max(4, min(96, budget // 4))
            instants = sorted({x for s in series for x in s.x if _instant(x) is not None})
            edges = [instants[int(i * len(instants) / cols)] for i in range(cols)] if instants else []
            z = []
            for s in series:
                sums = [0.0] * len(edges)
                cnt = [0] * len(edges)
                j = 0
                for x, y in sorted(((x, y) for x, y in zip(s.x, s.y, strict=False) if y is not None),
                                   key=lambda t: str(t[0])):
                    while j + 1 < len(edges) and str(x) >= str(edges[j + 1]):
                        j += 1
                    sums[j] += y
                    cnt[j] += 1
                z.append([round(a / c, 6) if c else None for a, c in zip(sums, cnt, strict=False)])
            doc["data"] = {"field": {"x": [str(e) for e in edges], "y": [s.name for s in series], "z": z,
                                     **({"unit": p.units[0]} if p.units else {})}}
    elif kind in ("gauge", "bullet", "progress") or (kind == "kpi" and norm.form == "value"):
        doc["data"] = {"value": _value_block(norm, p)}
    elif kind == "flame":
        def valued(node: Mapping[str, Any]) -> dict[str, Any]:
            kids = [valued(c) for c in node.get("children") or [] if isinstance(c, Mapping)]
            out: dict[str, Any] = {"name": str(node.get("name") or "(unnamed)"), "value": _tree_total(node)}
            if kids:
                out["children"] = kids
            return out

        tree = dict(norm.tree or {})
        unit = _unit(tree.get("unit"))
        doc["data"] = {"tree": {"root": valued(tree), **({"unit": unit} if unit else {})}}
    else:  # pragma: no cover - every chip in RULES is bound above
        raise SuggestError(f"no binding for {chip!r}")
    return doc


# ---------------------------------------------------------------------------
# The door.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Suggestion:
    chip: str
    kind: str
    label: str
    reason: str
    spec: dict[str, Any]

    def to_document(self) -> dict[str, Any]:
        return {"id": self.chip, "kind": self.kind, "label": self.label, "reason": self.reason,
                "spec": self.spec}


@dataclass(frozen=True)
class Suggestions:
    profile: DataProfile
    intents: tuple[str, ...]
    suggestions: tuple[Suggestion, ...]
    more: tuple[Suggestion, ...]
    invalid: tuple[Verdict, ...]
    ignored_preferences: tuple[str, ...] = ()
    quiet: str = ""

    def __bool__(self) -> bool:
        return bool(self.suggestions)

    def all(self) -> tuple[Suggestion, ...]:
        return self.suggestions + self.more


def _title(question: str, norm: Normal) -> str:
    if norm.form == "value":
        return str((norm.value or {}).get("label") or "Value")
    names = [s.name for s in norm.series]
    if norm.form == "hierarchy" and norm.tree:
        return str(norm.tree.get("name") or "Breakdown")
    if len(names) <= 3:
        return ", ".join(names) or "Chart"
    return f"{names[0]} and {len(names) - 1} more"


def suggest(
    result: Any,
    question: str = "",
    *,
    title: str = "",
    prefer: Sequence[str] = (),
    reference: Mapping[str, Any] | None = None,
    columns: Sequence[Any] = (),
    units: Mapping[str, str] | None = None,
    width: int = DEFAULT_WIDTH,
    top: int = TOP,
    chips: Sequence[str] | None = None,
) -> Suggestions:
    """Profile *result*, keep the kinds that can draw it, rank them for
    *question*, and bind a ChartSpec 2.0 for each.

    The top ``top`` are ``suggestions``; every other valid kind is in
    ``more``, so a reader can look at the same data another way without a
    second question. ``invalid`` says, for every kind left out, why.
    """
    norm = normalise(result, columns=columns, units=units)
    p = profile(norm, reference=reference)
    verdicts = compatible(p, chips=chips)
    valid = [v.chip for v in verdicts if v.valid]
    wanted = intents(question, p)
    ignored = tuple(c for c in prefer if c not in valid)
    ordered = rank(valid, wanted, p, prefer=prefer)
    head = pick_head(ordered, wanted, top=top, prefer=prefer)
    rest = [c for c in ordered if c not in head]
    budget = max(24, int(width * POINTS_PER_PX))
    name = title or _title(question, norm)
    reasons = {v.chip: v.reason for v in verdicts}

    def build(chips_: Sequence[str]) -> tuple[Suggestion, ...]:
        out = []
        for c in chips_:
            spec = _bind(c, norm, p, title=name, budget=budget)
            out.append(Suggestion(chip=c, kind=chip_kind(c), label=chip_label(c), reason=reasons[c], spec=spec))
        return tuple(out)

    quiet = ""
    if not head:
        quiet = ("nothing here draws honestly: " + "; ".join(sorted({v.reason for v in verdicts if not v.valid})[:2])
                 if verdicts else "no chart kinds are registered")
    return Suggestions(
        profile=p, intents=wanted, suggestions=build(head), more=build(rest),
        invalid=tuple(v for v in verdicts if not v.valid), ignored_preferences=ignored, quiet=quiet,
    )


def check_kind(result: Any, chip: str, *, reference: Mapping[str, Any] | None = None,
               columns: Sequence[Any] = (), units: Mapping[str, str] | None = None) -> str:
    """"" when *chip* can draw *result*, else the reason it cannot. For a
    request to switch kind ("show that as a box plot") the reason is the reply."""
    if chip not in RULES:
        known = ", ".join(registered_chips())
        return f"there is no chart kind {chip!r}; there is {known}"
    if chip not in registered_chips():
        return f"{chip} is not drawn by this build yet"
    p = profile(normalise(result, columns=columns, units=units), reference=reference)
    return RULES[chip][0](p)


# ---------------------------------------------------------------------------
# The wire block: what an answer carries.
# ---------------------------------------------------------------------------

BLOCK_SCHEMA = "axiom.chart_suggestions/1"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
                          .encode()).hexdigest()[:16]


def to_block(s: Suggestions, *, source: Mapping[str, Any] | None = None, question: str = "") -> dict[str, Any]:
    """The answer's chart block: profile, the suggestions and every other
    valid kind, with each spec's data factored into ``datasets`` by digest so
    kinds that draw the same numbers carry them once."""
    datasets: dict[str, Any] = {}

    def entry(sug: Suggestion) -> dict[str, Any]:
        spec = dict(sug.spec)
        data = spec.pop("data")
        key = _digest(data)
        datasets.setdefault(key, data)
        return {"id": sug.chip, "kind": sug.kind, "label": sug.label, "reason": sug.reason,
                "spec": spec, "data": key}

    block = {
        "kind": "chart_suggestions",
        "schema": BLOCK_SCHEMA,
        "question": question,
        "profile": s.profile.to_document(),
        "intents": list(s.intents),
        "suggestions": [entry(x) for x in s.suggestions],
        "more": [entry(x) for x in s.more],
        "invalid": [{"id": v.chip, "reason": v.reason} for v in s.invalid],
        "datasets": datasets,
    }
    if source:
        block["source"] = dict(source)
    return block


def block_bytes(block: Mapping[str, Any]) -> int:
    return len(json.dumps(block, separators=(",", ":"), default=str).encode())


def spec_of(block: Mapping[str, Any], chip: str) -> dict[str, Any] | None:
    """The whole ChartSpec for one chip of a block (the client does the same)."""
    for e in [*block.get("suggestions", []), *block.get("more", [])]:
        if e.get("id") == chip:
            return {**e["spec"], "data": block["datasets"][e["data"]]}
    return None


__all__ = [
    "BLOCK_SCHEMA", "DATA_CAP_BYTES", "INTENTS", "KIND_ORDER", "PREFERENCES", "RULES", "TOP", "VARIANTS",
    "DataProfile", "FieldProfile", "Normal", "SuggestError", "Suggestion", "Suggestions", "Verdict",
    "block_bytes", "check_kind", "chip_kind", "chip_label", "compatible", "intents", "normalise", "profile",
    "rank", "registered_chips", "spec_of", "suggest", "to_block",
]
