# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ChartSpec 2.0: one chart document for every chart the platform draws.

There were two vocabularies. :mod:`.chart_spec` (1.x) is a *query* document:
which site, which channels, what window, drawn as a time series. The data
platform's catalogue (:mod:`axiom.extensions.builtins.data_platform.charts`) is
a *registry entry*: an id, a kind from a closed list, a provenance claim, a
group and the gold verb that serves it. Neither could say "a pie of readings by
site", "a gauge against its limit" or "the heatmap a partner's Plotly figure
drew", and a chart imported from a Trame app had no document at all.

This is the one document all of them become (ADR-214). It is the same document
whether a person authored it, a model filled it in, or the importer read it
from a Plotly figure; the only difference is :data:`source`, which says which.
The renderer (:mod:`.chart_kinds`) draws a document and never asks where it
came from, which is what makes an imported chart indistinguishable from a
native one: they are the same object.

## The document

``schema_version``  ``"2.0"``. Required; absent or newer is a refusal.
``kind``            what sort of picture, from :data:`KINDS` (an open registry).
``title``           required: a chart nobody named is not citable.
``id``              optional stable slug, for saved views and re-import.
``subtitle``        optional second heading line.
``source``          ``{"type": "native"}`` (the default) or ``{"type":
                    "imported", "format": "plotly"|"trame", "figure": <ref>,
                    "digest": "sha256:…", "importer_version": "…",
                    "revision": n}``. An import keeps the original figure as
                    provenance (``figure`` names where it is held, ``digest``
                    pins which bytes); a re-import replaces ``data`` and bumps
                    ``revision`` under the same ``id``.
``data``            exactly one of
                    ``query``  — the 1.x nucleus ``{site, channels, window,
                    transform}``: drawn from gold by a figure source;
                    ``verb``   — ``{name, params}``: a served gold verb;
                    ``series`` — inline ``[{name, unit, x, y, …}]``;
                    ``field``  — inline ``{x, y, z, unit}`` for a heatmap;
                    ``value``  — inline ``{value, unit, min, max, target,
                    trend}`` for a KPI, gauge, progress bar or bullet;
                    plus optional ``frames`` — ``{key, items: [{value, label,
                    data}]}`` — data variants a control chooses between.
``axes``            ``{x, y, y2}``, each ``{type, title, unit, range}``;
                    ``type`` is ``time``, ``linear``, ``log`` or ``category``.
``encoding``        kind options: ``stack`` (none/stacked/percent),
                    ``orientation``, ``bins``, ``fit``, ``hole``, ``curve``,
                    ``colormap``, ``show_points``, ``compare``.
``marks``           annotations drawn on the plot: ``band`` (a range on one
                    axis, e.g. a limit), ``rule`` (one value, e.g. a setpoint
                    or an event), ``annotation`` (text at a point). Each may
                    name a ``layer`` a toggle shows or hides.
``layout``          ``{panels: [{id, title}], columns}`` for subplots and small
                    multiples; a series names its ``panel``.
``controls``        widgets declared with the chart and driving its state:
                    ``slider``, ``range``, ``dropdown``, ``toggle``,
                    ``segmented``, ``playback``. Each ``bind``\\ s to one target:
                    ``frame`` (which data variant), ``group`` (which series
                    group is shown), ``layer`` (whether a mark layer is
                    shown), ``window`` (the x range drawn), or ``param`` (a
                    value passed to the data source).
``catalog``         the registry half: ``group``, ``order``, ``default``,
                    ``provenance``, ``roles``, ``verb``, ``unit_authority``,
                    ``surfaces``, ``origin``.
``display_units``, ``series_colours``, ``palette``  as in 1.x, plus the palette
                    name from :mod:`.palettes`.

A chart's *state* (the value of each control) is not part of the document: the
document says which controls exist and what each starts at; a reader, a chat
action or a link moves them. :func:`resolve_state` merges a state into the
declared defaults and refuses a value a control cannot take.

## What this module is not

No renderer, no store, no network. Like 1.x it names no domain noun: a site, a
channel and a unit are opaque strings it carries and never interprets.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: The schema this build reads and writes.
SCHEMA_VERSION = "2.0"
_MAJOR, _MINOR = (int(p) for p in SCHEMA_VERSION.split("."))

_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
_VERSION = re.compile(r"^(\d+)\.(\d+)$")

#: Every key a document may carry, in canonical order.
DOCUMENT_KEYS = (
    "schema_version", "id", "kind", "title", "subtitle", "source", "data", "axes",
    "encoding", "marks", "layout", "controls", "catalog", "display_units",
    "series_colours", "palette",
)

AXIS_TYPES = ("time", "linear", "log", "category")
AXIS_KEYS = frozenset({"type", "title", "unit", "range", "side"})
DATA_SHAPES = ("query", "verb", "series", "field", "value", "tree", "scene")
SERIES_KEYS = frozenset({
    "name", "unit", "x", "y", "modelled", "against", "axis", "panel", "group",
    "role", "channel", "colour", "low", "high",
})
MARK_TYPES = ("band", "rule", "annotation")
MARK_KEYS = frozenset({
    "type", "axis", "from", "to", "value", "x", "y", "text", "label", "tone",
    "layer", "panel",
})
TONES = ("neutral", "info", "warn", "alarm", "ok")
CONTROL_TYPES = ("slider", "range", "dropdown", "toggle", "segmented", "playback", "text", "path")
BIND_TARGETS = ("frame", "group", "layer", "window", "param", "encoding", "search", "zoom")
CONTROL_KEYS = frozenset({
    "id", "type", "label", "bind", "options", "min", "max", "step", "value",
    "interval_ms", "unit",
})
STACKS = ("none", "stacked", "percent")
ORIENTATIONS = ("vertical", "horizontal")
FITS = ("none", "linear")
CURVES = ("linear", "step")
PROVENANCES = ("measured", "model_derived", "hybrid")
SOURCE_FORMATS = ("plotly", "trame")
ENCODING_KEYS = frozenset({
    "stack", "orientation", "bins", "fit", "hole", "curve", "colormap",
    "show_points", "compare", "labels", "aspect", "direction", "invert", "colour_by",
    "camera", "opacity", "edges", "explode", "cutaway", "slice_axis", "slice_at", "clip", "scalar", "iso", "pick",
})
CAMERA_PRESETS = ("iso", "top", "side", "front")
#: Encoding of a 3D kind.
SCENE_ENCODING = frozenset({"colormap", "camera", "opacity", "edges", "explode", "cutaway", "slice_axis",
                            "slice_at", "clip", "scalar", "pick", "aspect"})
DIRECTIONS = ("flame", "icicle")
COLOUR_BY = ("name", "category")
#: Encoding fields a control may override (bind.to == "encoding").
CONTROLLABLE_ENCODING = {"direction": DIRECTIONS, "invert": (True, False), "colour_by": COLOUR_BY,
                         "stack": STACKS, "orientation": ORIENTATIONS, "camera": CAMERA_PRESETS,
                         "slice_at": None, "scalar": None, "opacity": None, "explode": None,
                         "cutaway": (True, False), "edges": (True, False), "pick": None}
CATALOG_KEYS = frozenset({
    "group", "order", "default", "provenance", "roles", "verb", "unit_authority",
    "surfaces", "origin", "notes", "channels",
})


class UnifiedSpecError(ValueError):
    """A document that is not a ChartSpec 2.0, and every reason why."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("chart spec 2.0: " + "; ".join(self.problems))


@dataclass(frozen=True)
class Kind:
    """One sort of picture and what it may be drawn from.

    ``data`` names the inline/remote data shapes it accepts. ``cartesian``
    kinds take ``axes``, ``marks`` and ``layout.panels``; the rest (a pie, a
    gauge, a tile) have no axes and refuse them, because an axis title that
    draws nothing would diff as though it mattered.
    """

    name: str
    summary: str
    data: frozenset[str]
    cartesian: bool = True
    encoding: frozenset[str] = frozenset()


_KINDS: dict[str, Kind] = {}


def register_kind(kind: Kind) -> None:
    """Add a kind. A second definition under one name is refused, not overridden."""
    existing = _KINDS.get(kind.name)
    if existing is not None and existing != kind:
        raise UnifiedSpecError([f"kind {kind.name!r} is already registered differently"])
    _KINDS[kind.name] = kind


def registered_kinds() -> tuple[str, ...]:
    return tuple(_KINDS)


def lookup_kind(name: str) -> Kind:
    try:
        return _KINDS[name]
    except KeyError:
        raise UnifiedSpecError([
            f"kind {name!r} is not registered; registered: {', '.join(_KINDS)}"
        ]) from None


_SERIESY = frozenset({"query", "verb", "series"})
for _k in (
    Kind("line", "Values joined in order: over time, or against a numeric axis "
         "(a spectrum on log-log axes).", _SERIESY, encoding=frozenset({"curve", "show_points", "compare"})),
    Kind("area", "A line filled to its baseline; stacked, parts sum to a whole.",
         _SERIESY, encoding=frozenset({"stack", "curve"})),
    Kind("step", "A value held until the next reading: a setpoint, a state, a count.",
         _SERIESY),
    Kind("sparkline", "A line with no axes, for a value's recent shape at a glance.",
         _SERIESY, cartesian=False),
    Kind("bar", "One bar per category; grouped, stacked or stacked to 100%, "
         "vertical or horizontal.", _SERIESY,
         encoding=frozenset({"stack", "orientation", "labels"})),
    Kind("bullet", "A value against its target and qualitative ranges, in one bar.",
         _SERIESY | {"value"}, cartesian=False),
    Kind("pie", "Parts of one whole, as slices.", _SERIESY, cartesian=False,
         encoding=frozenset({"hole", "labels"})),
    Kind("donut", "Parts of one whole, as a ring with the total in the middle.",
         _SERIESY, cartesian=False, encoding=frozenset({"hole", "labels"})),
    Kind("progress", "How far a value is between its minimum and maximum, as a bar; "
         "one row per series.", _SERIESY | {"value"}, cartesian=False),
    Kind("gauge", "A value on a ring or arc between its minimum and maximum, "
         "with its limit.", frozenset({"value"}), cartesian=False),
    Kind("kpi", "One current value, its unit, and its trend; a strip of tiles, one "
         "per series.", _SERIESY | {"value"}, cartesian=False),
    Kind("histogram", "How one value is distributed, in bins.", _SERIESY,
         encoding=frozenset({"bins", "orientation"})),
    Kind("box", "Median, quartiles and range of one value per group.", _SERIESY,
         encoding=frozenset({"orientation", "show_points"})),
    Kind("scatter", "One value against another, optionally with a fitted line.",
         _SERIESY, encoding=frozenset({"fit"})),
    Kind("heatmap", "A scalar over a plane, on a sequential colour scale.",
         frozenset({"field"}), encoding=frozenset({"colormap", "aspect", "labels"})),
    Kind("state", "A categorical channel as spans over time.", _SERIESY),
    Kind("flame", "A hierarchy of named values as stacked frames, width by value: a flame "
         "graph (root at the bottom) or an icicle (root at the top).", frozenset({"tree"}),
         cartesian=False, encoding=frozenset({"direction", "invert", "colour_by"})),
    Kind("mesh3d", "Surfaces and meshes in 3D, coloured by a per-cell or per-point scalar.",
         frozenset({"scene"}), cartesian=False, encoding=SCENE_ENCODING),
    Kind("surface", "z over x and y as a shaded 3D surface.", frozenset({"scene"}), cartesian=False,
         encoding=SCENE_ENCODING),
    Kind("scatter3d", "Points in 3D, coloured by a scalar.", frozenset({"scene"}), cartesian=False,
         encoding=SCENE_ENCODING),
    Kind("volume", "A scalar field over a 3D grid: slices, isosurfaces and opacity.", frozenset({"scene"}),
         cartesian=False, encoding=SCENE_ENCODING | {"iso"}),
):
    register_kind(_k)


# ---------------------------------------------------------------------------
# Checking. Every problem is collected, never just the first.
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _plain(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != "" and "\n" not in value


def _unknown(where: str, raw: Mapping[str, Any], allowed: frozenset[str] | Sequence[str],
             problems: list[str]) -> None:
    extra = sorted(set(raw) - set(allowed))
    if extra:
        problems.append(f"{where}: unknown key(s) {', '.join(extra)}")


def _check_source(raw: Any, problems: list[str]) -> None:
    if not isinstance(raw, Mapping):
        problems.append("source: must be a table")
        return
    kind = raw.get("type")
    if kind == "native":
        _unknown("source", raw, {"type", "author"}, problems)
        return
    if kind != "imported":
        problems.append("source.type: must be 'native' or 'imported'")
        return
    _unknown("source", raw, {"type", "format", "figure", "digest", "importer_version",
                             "revision", "refused", "replaced", "trace_map"}, problems)
    if raw.get("format") not in SOURCE_FORMATS:
        problems.append(f"source.format: must be one of {', '.join(SOURCE_FORMATS)}")
    if not _plain(raw.get("figure")):
        problems.append("source.figure: an import names where its original figure is held")
    if not (isinstance(raw.get("digest"), str) and raw["digest"].startswith("sha256:")):
        problems.append("source.digest: an import pins the original's bytes as sha256:<hex>")
    if not _plain(raw.get("importer_version")):
        problems.append("source.importer_version: required on an import")
    rev = raw.get("revision", 1)
    if not (isinstance(rev, int) and not isinstance(rev, bool) and rev >= 1):
        problems.append("source.revision: a positive integer")


def _check_series(where: str, raw: Any, problems: list[str], *, panels: set[str]) -> None:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        problems.append(f"{where}: a non-empty list of series")
        return
    names: set[str] = set()
    for i, s in enumerate(raw):
        at = f"{where}[{i}]"
        if not isinstance(s, Mapping):
            problems.append(f"{at}: must be a table")
            continue
        _unknown(at, s, SERIES_KEYS, problems)
        name = s.get("name")
        if not _plain(name):
            problems.append(f"{at}.name: every series is named; a colour is not a name")
        elif name in names:
            problems.append(f"{at}.name: {name!r} twice would be drawn twice and read as one")
        else:
            names.add(name)
        ys = s.get("y")
        if not isinstance(ys, Sequence) or isinstance(ys, (str, bytes)):
            problems.append(f"{at}.y: a list of numbers (null for a gap)")
        else:
            if any(v is not None and not _is_number(v) for v in ys):
                problems.append(f"{at}.y: only numbers and null")
            xs = s.get("x")
            if xs is not None and (not isinstance(xs, Sequence) or isinstance(xs, (str, bytes))
                                   or len(xs) != len(ys)):
                problems.append(f"{at}.x: as many x values as y values")
        if s.get("axis", "y") not in ("y", "y2"):
            problems.append(f"{at}.axis: 'y' or 'y2'")
        if "panel" in s and s["panel"] not in panels:
            problems.append(f"{at}.panel: no panel {s['panel']!r} in layout.panels")
        for flag in ("modelled",):
            if flag in s and not isinstance(s[flag], bool):
                problems.append(f"{at}.{flag}: true or false")


def _check_value(raw: Any, problems: list[str]) -> None:
    if not isinstance(raw, Mapping):
        problems.append("data.value: must be a table")
        return
    _unknown("data.value", raw, {"value", "unit", "min", "max", "target", "trend",
                                 "ranges", "label", "delta", "format", "at"}, problems)
    if raw.get("value") is not None and not _is_number(raw.get("value")):
        problems.append("data.value.value: a number (or null when there is no reading)")
    for k in ("min", "max", "target", "delta"):
        if k in raw and raw[k] is not None and not _is_number(raw[k]):
            problems.append(f"data.value.{k}: a number")
    if _is_number(raw.get("min")) and _is_number(raw.get("max")) and raw["min"] >= raw["max"]:
        problems.append("data.value: min must be below max")
    trend = raw.get("trend")
    if trend is not None and (not isinstance(trend, Sequence) or isinstance(trend, str)
                              or any(v is not None and not _is_number(v) for v in trend)):
        problems.append("data.value.trend: a list of numbers")
    ranges = raw.get("ranges")
    if ranges is not None and (not isinstance(ranges, Sequence) or isinstance(ranges, str)
                               or not all(_is_number(v) for v in ranges)):
        problems.append("data.value.ranges: a list of numbers (qualitative band edges)")


def _check_field(raw: Any, problems: list[str]) -> None:
    if not isinstance(raw, Mapping):
        problems.append("data.field: must be a table")
        return
    _unknown("data.field", raw, {"x", "y", "z", "unit", "name", "zmin", "zmax"}, problems)
    z = raw.get("z")
    if not isinstance(z, Sequence) or not z or not all(isinstance(r, Sequence) for r in z):
        problems.append("data.field.z: a list of rows")
        return
    width = len(z[0])
    if any(len(r) != width for r in z):
        problems.append("data.field.z: every row the same length")
    if any(v is not None and not _is_number(v) for r in z for v in r):
        problems.append("data.field.z: only numbers and null")
    for axis, n in (("x", width), ("y", len(z))):
        vals = raw.get(axis)
        if vals is not None and (not isinstance(vals, Sequence) or len(vals) != n):
            problems.append(f"data.field.{axis}: {n} values, one per {'column' if axis == 'x' else 'row'}")


def _check_data_block(where: str, raw: Any, kind: Kind, problems: list[str],
                      *, panels: set[str], allow_frames: bool) -> None:
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: must be a table")
        return
    allowed = set(DATA_SHAPES) | ({"frames"} if allow_frames else set())
    _unknown(where, raw, allowed, problems)
    shapes = [k for k in DATA_SHAPES if k in raw]
    if len(shapes) != 1:
        problems.append(f"{where}: exactly one of {', '.join(DATA_SHAPES)} (got {', '.join(shapes) or 'none'})")
        return
    shape = shapes[0]
    if shape not in kind.data:
        problems.append(f"{where}: a {kind.name} is drawn from {', '.join(sorted(kind.data))}, not {shape}")
    if shape == "series":
        _check_series(f"{where}.series", raw["series"], problems, panels=panels)
    elif shape == "value":
        _check_value(raw["value"], problems)
    elif shape == "field":
        _check_field(raw["field"], problems)
    elif shape == "query":
        q = raw["query"]
        if not isinstance(q, Mapping) or not _plain(q.get("site")) or not q.get("channels"):
            problems.append(f"{where}.query: names a site and at least one channel")
        else:
            _unknown(f"{where}.query", q, {"site", "channels", "window", "transform", "last"}, problems)
    elif shape == "tree":
        t = raw["tree"]
        forms = [k for k in ("root", "folded", "rows") if isinstance(t, Mapping) and k in t]
        if len(forms) != 1:
            problems.append(f"{where}.tree: exactly one of root (a nested tree), folded (stack;stack count "
                            "lines) or rows (id, parent, name, value)")
        else:
            _unknown(f"{where}.tree", t, {"root", "folded", "rows", "name", "unit"}, problems)
            from .flame_data import HierarchyError, read
            try:
                read(t)
            except HierarchyError as exc:
                problems.append(f"{where}.tree: {exc}")
    elif shape == "scene":
        from .scene_data import check

        problems.extend(f"{where}.{p[5:]}" if p.startswith("data.") else p for p in check(raw["scene"]))
    elif shape == "verb":
        v = raw["verb"]
        if not isinstance(v, Mapping) or not _plain(v.get("name")):
            problems.append(f"{where}.verb: names a served verb")


def _check_frames(raw: Any, kind: Kind, problems: list[str], *, panels: set[str]) -> list[Any]:
    if not isinstance(raw, Mapping):
        problems.append("data.frames: must be a table")
        return []
    _unknown("data.frames", raw, {"key", "items"}, problems)
    if not _plain(raw.get("key")):
        problems.append("data.frames.key: names what the frames vary over")
    items = raw.get("items")
    if not isinstance(items, Sequence) or not items:
        problems.append("data.frames.items: at least one frame")
        return []
    values: list[Any] = []
    for i, item in enumerate(items):
        if not isinstance(item, Mapping):
            problems.append(f"data.frames.items[{i}]: must be a table")
            continue
        _unknown(f"data.frames.items[{i}]", item, {"value", "label", "data"}, problems)
        values.append(item.get("value"))
        _check_data_block(f"data.frames.items[{i}].data", item.get("data"), kind, problems,
                          panels=panels, allow_frames=False)
    if len({json.dumps(v, sort_keys=True) for v in values}) != len(values):
        problems.append("data.frames.items: two frames with one value cannot be told apart")
    return values


def _check_controls(raw: Any, problems: list[str], *, frames: list[Any], groups: set[str],
                    layers: set[str]) -> None:
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        problems.append("controls: a list")
        return
    ids: set[str] = set()
    for i, c in enumerate(raw):
        at = f"controls[{i}]"
        if not isinstance(c, Mapping):
            problems.append(f"{at}: must be a table")
            continue
        _unknown(at, c, CONTROL_KEYS, problems)
        cid = c.get("id")
        if not (isinstance(cid, str) and _SLUG.match(cid)):
            problems.append(f"{at}.id: a slug a chat action can name")
        elif cid in ids:
            problems.append(f"{at}.id: {cid!r} twice")
        else:
            ids.add(cid)
        ctype = c.get("type")
        if ctype not in CONTROL_TYPES:
            problems.append(f"{at}.type: one of {', '.join(CONTROL_TYPES)}")
            continue
        if not _plain(c.get("label")):
            problems.append(f"{at}.label: a control a person cannot name, they cannot operate")
        bind = c.get("bind")
        if not isinstance(bind, Mapping) or bind.get("to") not in BIND_TARGETS:
            problems.append(f"{at}.bind.to: one of {', '.join(BIND_TARGETS)}")
            continue
        target = bind["to"]
        if target == "frame" and not frames:
            problems.append(f"{at}: binds a frame but data has no frames")
        if target == "group" and not groups:
            problems.append(f"{at}: binds a series group but no series names a group")
        if target == "layer":
            layer = bind.get("key")
            if layer not in layers:
                problems.append(f"{at}: binds layer {layer!r}, which no mark carries")
        if target == "param" and not _plain(bind.get("key")):
            problems.append(f"{at}.bind.key: a param control names the parameter")
        if target == "encoding" and bind.get("key") not in CONTROLLABLE_ENCODING:
            problems.append(f"{at}.bind.key: an encoding control names one of "
                            f"{', '.join(CONTROLLABLE_ENCODING)}")
        if target == "search" and ctype != "text":
            problems.append(f"{at}: a search is typed into a text control")
        if target == "zoom" and ctype != "path":
            problems.append(f"{at}: a zoom is a path control (set by clicking a frame)")
        if ctype in ("dropdown", "segmented"):
            opts = c.get("options")
            if not isinstance(opts, Sequence) or not opts:
                problems.append(f"{at}.options: at least one option")
        if ctype in ("slider", "playback") and target != "frame":
            if not (_is_number(c.get("min")) and _is_number(c.get("max"))):
                problems.append(f"{at}: a slider over values names min and max")
        if ctype == "toggle" and "value" in c and not isinstance(c["value"], bool):
            problems.append(f"{at}.value: a toggle starts true or false")


def _marks_layers(raw: Any, problems: list[str], *, panels: set[str], cartesian: bool) -> set[str]:
    layers: set[str] = set()
    if raw is None:
        return layers
    if not cartesian:
        problems.append("marks: this kind has no axes to mark")
        return layers
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        problems.append("marks: a list")
        return layers
    for i, m in enumerate(raw):
        at = f"marks[{i}]"
        if not isinstance(m, Mapping):
            problems.append(f"{at}: must be a table")
            continue
        _unknown(at, m, MARK_KEYS, problems)
        mtype = m.get("type")
        if mtype not in MARK_TYPES:
            problems.append(f"{at}.type: one of {', '.join(MARK_TYPES)}")
            continue
        if m.get("tone", "neutral") not in TONES:
            problems.append(f"{at}.tone: one of {', '.join(TONES)}")
        if mtype in ("band", "rule") and m.get("axis", "y") not in ("x", "y", "y2"):
            problems.append(f"{at}.axis: x, y or y2")
        if mtype == "band" and ("from" not in m or "to" not in m):
            problems.append(f"{at}: a band has from and to")
        if mtype == "rule" and "value" not in m:
            problems.append(f"{at}: a rule has a value")
        if mtype == "annotation" and not _plain(m.get("text")):
            problems.append(f"{at}.text: an annotation says something")
        if "panel" in m and m["panel"] not in panels:
            problems.append(f"{at}.panel: no panel {m['panel']!r}")
        if "layer" in m:
            if not _plain(m["layer"]):
                problems.append(f"{at}.layer: a name")
            else:
                layers.add(m["layer"])
    return layers


def validate(document: Any) -> list[str]:
    """Every reason *document* is not a ChartSpec 2.0. Empty means it is one."""
    problems: list[str] = []
    if not isinstance(document, Mapping):
        return ["a chart spec is a table"]
    version = document.get("schema_version")
    match = _VERSION.match(version) if isinstance(version, str) else None
    if not match:
        return ["schema_version: required, as major.minor"]
    major, minor = int(match.group(1)), int(match.group(2))
    if major != _MAJOR or minor > _MINOR:
        return [f"schema_version {version} is not one this build reads ({SCHEMA_VERSION}); "
                "a 1.x document is migrated with unified_migrate.from_v1"]
    _unknown("chart spec", document, DOCUMENT_KEYS, problems)
    name = document.get("kind")
    if name not in _KINDS:
        problems.append(f"kind: {name!r} is not registered ({', '.join(_KINDS)})")
        return problems
    kind = _KINDS[name]
    if not _plain(document.get("title")):
        problems.append("title: required; a chart nobody named cannot be cited")
    if "id" in document and not (isinstance(document["id"], str) and _SLUG.match(document["id"])):
        problems.append("id: a lowercase slug")
    if "source" in document:
        _check_source(document["source"], problems)

    layout = document.get("layout")
    panels: set[str] = set()
    if layout is not None:
        if not kind.cartesian:
            problems.append("layout: panels are for kinds with axes")
        elif not isinstance(layout, Mapping):
            problems.append("layout: must be a table")
        else:
            _unknown("layout", layout, {"panels", "columns", "share_x"}, problems)
            for i, p in enumerate(layout.get("panels") or []):
                if not isinstance(p, Mapping) or not _plain(p.get("id")):
                    problems.append(f"layout.panels[{i}]: has an id")
                    continue
                _unknown(f"layout.panels[{i}]", p, {"id", "title", "axes"}, problems)
                panels.add(p["id"])
            cols = layout.get("columns", 1)
            if not (isinstance(cols, int) and 1 <= cols <= 6):
                problems.append("layout.columns: 1 to 6")

    data = document.get("data")
    frames: list[Any] = []
    groups: set[str] = set()
    if data is None:
        problems.append("data: required")
    elif isinstance(data, Mapping):
        if "frames" in data:
            frames = _check_frames(data["frames"], kind, problems, panels=panels)
            rest = {k: v for k, v in data.items() if k != "frames"}
            if rest:
                _check_data_block("data", rest, kind, problems, panels=panels, allow_frames=False)
        else:
            _check_data_block("data", data, kind, problems, panels=panels, allow_frames=False)
        blocks = [data] + [it.get("data") or {} for it in (data.get("frames") or {}).get("items") or []
                           if isinstance(it, Mapping)]
        for block in blocks:
            for s in (block.get("series") or []) if isinstance(block, Mapping) else []:
                if isinstance(s, Mapping) and _plain(s.get("group")):
                    groups.add(s["group"])
    else:
        problems.append("data: must be a table")

    axes = document.get("axes")
    if axes is not None:
        if not kind.cartesian:
            problems.append(f"axes: a {kind.name} has no axes")
        elif not isinstance(axes, Mapping):
            problems.append("axes: must be a table")
        else:
            _unknown("axes", axes, {"x", "y", "y2"}, problems)
            for key, axis in axes.items():
                if not isinstance(axis, Mapping):
                    problems.append(f"axes.{key}: must be a table")
                    continue
                _unknown(f"axes.{key}", axis, AXIS_KEYS, problems)
                if "type" in axis and axis["type"] not in AXIS_TYPES:
                    problems.append(f"axes.{key}.type: one of {', '.join(AXIS_TYPES)}")
                rng = axis.get("range")
                if rng is not None and not (isinstance(rng, Sequence) and len(rng) == 2):
                    problems.append(f"axes.{key}.range: [low, high]")

    encoding = document.get("encoding")
    if encoding is not None:
        if not isinstance(encoding, Mapping):
            problems.append("encoding: must be a table")
        else:
            _unknown("encoding", encoding, ENCODING_KEYS, problems)
            surplus = sorted(set(encoding) - kind.encoding)
            if surplus:
                problems.append(f"encoding: a {kind.name} has no use for {', '.join(surplus)}")
            checks = (("stack", STACKS), ("orientation", ORIENTATIONS), ("fit", FITS),
                      ("curve", CURVES), ("direction", DIRECTIONS), ("colour_by", COLOUR_BY))
            for key, allowed in checks:
                if key in encoding and encoding[key] not in allowed:
                    problems.append(f"encoding.{key}: one of {', '.join(allowed)}")
            if "invert" in encoding and not isinstance(encoding["invert"], bool):
                problems.append("encoding.invert: true or false")
            cam = encoding.get("camera")
            if cam is not None and not (cam in CAMERA_PRESETS or (isinstance(cam, Mapping)
                                                                  and set(cam) <= {"azimuth", "elevation", "zoom"})):
                problems.append(f"encoding.camera: one of {', '.join(CAMERA_PRESETS)}, or {{azimuth, elevation}}")
            if "opacity" in encoding and not (_is_number(encoding["opacity"]) and 0 < encoding["opacity"] <= 1):
                problems.append("encoding.opacity: above 0, at most 1")
            if "explode" in encoding and not (_is_number(encoding["explode"]) and 0 <= encoding["explode"] <= 3):
                problems.append("encoding.explode: 0 to 3")
            if encoding.get("slice_axis", "z") not in ("x", "y", "z"):
                problems.append("encoding.slice_axis: x, y or z")
            if "bins" in encoding and not (isinstance(encoding["bins"], int) and 1 <= encoding["bins"] <= 400):
                problems.append("encoding.bins: 1 to 400")
            if "hole" in encoding and not (_is_number(encoding["hole"]) and 0 <= encoding["hole"] < 0.95):
                problems.append("encoding.hole: 0 to 0.95")

    layers = _marks_layers(document.get("marks"), problems, panels=panels, cartesian=kind.cartesian)
    if "controls" in document:
        _check_controls(document["controls"], problems, frames=frames, groups=groups, layers=layers)

    catalog = document.get("catalog")
    if catalog is not None:
        if not isinstance(catalog, Mapping):
            problems.append("catalog: must be a table")
        else:
            _unknown("catalog", catalog, CATALOG_KEYS, problems)
            if "provenance" in catalog and catalog["provenance"] not in PROVENANCES:
                problems.append(f"catalog.provenance: one of {', '.join(PROVENANCES)}")
            series = (data or {}).get("series") if isinstance(data, Mapping) else None
            if catalog.get("provenance") == "measured" and any(
                isinstance(s, Mapping) and s.get("modelled") for s in series or []
            ):
                problems.append("catalog.provenance: 'measured' while a series is modelled; "
                                "a model must never render as a measurement")
    for key in ("display_units", "series_colours"):
        value = document.get(key)
        if value is not None and not (isinstance(value, Mapping) and value
                                      and all(_plain(k) and _plain(v) for k, v in value.items())):
            problems.append(f"{key}: a non-empty table of name to value")
    if "palette" in document and not _plain(document["palette"]):
        problems.append("palette: a palette name")
    return problems


def parse(document: Any) -> dict[str, Any]:
    """*document* as a canonical ChartSpec 2.0, or :class:`UnifiedSpecError`.

    Canonical means key order fixed and ``source`` defaulted to native, so two
    spellings of one chart are one document and diff as one.
    """
    problems = validate(document)
    if problems:
        raise UnifiedSpecError(problems)
    out: dict[str, Any] = {}
    for key in DOCUMENT_KEYS:
        if key in document:
            out[key] = copy.deepcopy(document[key])
    out.setdefault("source", {"type": "native"})
    # ``source`` sits after ``subtitle`` in canonical order even when defaulted.
    return {k: out[k] for k in DOCUMENT_KEYS if k in out}


def canonical_json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, sort_keys=False, ensure_ascii=False, separators=(",", ":"))


def spec_id(document: Mapping[str, Any]) -> str:
    """A content id for citing a chart: the document minus its provenance.

    ``source`` and ``id`` are left out deliberately: where a chart came from
    and what a saved view calls it are not what it shows. An imported chart and
    the same chart authored natively are the same chart; their content ids
    agree, which is the seamlessness claim stated as a hash.
    """
    body = _canonical_numbers({k: v for k, v in document.items() if k not in ("source", "id")})
    text = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"axiom://chart-spec/2/{digest}"


def _canonical_numbers(value: Any) -> Any:
    """``1e3`` and ``1000`` are one number; a content id must not tell them apart."""
    if isinstance(value, Mapping):
        return {k: _canonical_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_canonical_numbers(v) for v in value]
    if isinstance(value, float) and value.is_integer() and abs(value) < 2**53:
        return int(value)
    return value


def figure_digest(figure: Any) -> str:
    """``sha256:<hex>`` of a figure's canonical JSON, for ``source.digest``."""
    text = json.dumps(figure, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# State: what the controls are set to.
# ---------------------------------------------------------------------------


@dataclass
class ResolvedState:
    """Every control's value, and what the renderer must do because of them."""

    values: dict[str, Any] = field(default_factory=dict)
    #: The frame value to draw, when a control chooses one.
    frame: Any = None
    #: Series groups to show; ``None`` means all.
    groups: set[str] | None = None
    #: Mark layers hidden.
    hidden_layers: set[str] = field(default_factory=set)
    #: The x range to draw, when a range control holds one.
    window: tuple[Any, Any] | None = None
    #: Parameters for a data source.
    params: dict[str, Any] = field(default_factory=dict)
    #: Encoding fields a control overrides.
    encoding: dict[str, Any] = field(default_factory=dict)
    #: The search a text control holds.
    search: str = ""
    #: The frame a path control zoomed into, as names below the root.
    zoom: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _option_values(control: Mapping[str, Any], frames: list[Any]) -> list[Any]:
    if control.get("options"):
        return [o.get("value") if isinstance(o, Mapping) else o for o in control["options"]]
    if control.get("bind", {}).get("to") == "frame":
        return list(frames)
    return []


def default_value(control: Mapping[str, Any], frames: list[Any]) -> Any:
    """What a control starts at when the document does not say."""
    if "value" in control:
        return control["value"]
    ctype = control["type"]
    if ctype == "toggle":
        return True
    if ctype in ("text", "path"):
        return ""
    if ctype == "range":
        return [control.get("min"), control.get("max")]
    options = _option_values(control, frames)
    if options:
        return options[0]
    return control.get("min")


def frames_of(document: Mapping[str, Any]) -> list[Any]:
    data = document.get("data") or {}
    return [it.get("value") for it in ((data.get("frames") or {}).get("items") or [])]


def resolve_state(document: Mapping[str, Any], state: Mapping[str, Any] | None = None) -> ResolvedState:
    """Merge *state* into the declared defaults; refuse what a control cannot take.

    A refused value is not applied and is reported in ``notes``: a chat action
    that asks for the 40 cm plane on a chart that stops at 38 is answered, not
    silently clamped.
    """
    state = dict(state or {})
    frames = frames_of(document)
    out = ResolvedState()
    controls = document.get("controls") or []
    known = {c.get("id") for c in controls}
    for cid in sorted(set(state) - known):
        out.notes.append(f"no control {cid!r} on this chart")
    # Controls bound to one target are views of ONE value: a slider and a
    # playback over the same frames must never show two planes. The group
    # starts at its first member's default, and takes the first value any
    # member was given that a member can take.
    groups: dict[tuple[str, Any], list[Mapping[str, Any]]] = {}
    for c in controls:
        groups.setdefault((c["bind"]["to"], c["bind"].get("key")), []).append(c)
    shared: dict[tuple[str, Any], Any] = {}
    for key, members in groups.items():
        value = default_value(members[0], frames)
        for c in members:
            if c["id"] in state:
                ok, why = _admissible(c, state[c["id"]], frames)
                if ok:
                    value = state[c["id"]]
                    break
                out.notes.append(f"{c['label']}: {why}")
        shared[key] = value
    for c in controls:
        cid = c["id"]
        value = shared[(c["bind"]["to"], c["bind"].get("key"))]
        out.values[cid] = value
        bind = c["bind"]
        target = bind["to"]
        if target == "frame":
            out.frame = value
        elif target == "group":
            if c["type"] == "toggle":
                key = bind.get("key")
                if out.groups is None:
                    out.groups = set()
                    for s in _all_series(document):
                        if s.get("group"):
                            out.groups.add(s["group"])
                if not value and key:
                    out.groups.discard(key)
            else:
                out.groups = {value} if not isinstance(value, list) else set(value)
        elif target == "layer":
            if not value:
                out.hidden_layers.add(bind["key"])
        elif target == "window":
            if isinstance(value, Sequence) and not isinstance(value, str) and len(value) == 2:
                out.window = (value[0], value[1])
        elif target == "param":
            out.params[bind["key"]] = value
        elif target == "encoding":
            out.encoding[bind["key"]] = value
        elif target == "search":
            out.search = str(value or "")
        elif target == "zoom":
            out.zoom = path_of(value)
    return out


def path_of(value: Any) -> list[str]:
    """A frame path as names: ``"a;b;c"`` or ``["a", "b", "c"]``."""
    if isinstance(value, str):
        return [p for p in value.split(";") if p]
    if isinstance(value, Sequence):
        return [str(p) for p in value if str(p)]
    return []


def _all_series(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = document.get("data") or {}
    out = list(data.get("series") or [])
    for it in (data.get("frames") or {}).get("items") or []:
        out.extend((it.get("data") or {}).get("series") or [])
    return out


def _admissible(control: Mapping[str, Any], value: Any, frames: list[Any]) -> tuple[bool, str]:
    ctype = control["type"]
    if ctype == "toggle":
        return (isinstance(value, bool), "a toggle is on or off")
    if ctype == "text":
        ok = isinstance(value, str) and len(value) <= 200 and "\n" not in value
        return ok, "a search is one line of text"
    if ctype == "path":
        ok = isinstance(value, str) or (isinstance(value, Sequence) and all(isinstance(p, str) for p in value))
        return ok, "a path is frame names, as 'a;b;c' or a list"
    options = _option_values(control, frames)
    if ctype in ("dropdown", "segmented") or (ctype in ("slider", "playback") and options):
        if value in options:
            return True, ""
        shown = ", ".join(str(o) for o in options[:8])
        return False, f"{value!r} is not one of its values ({shown})"
    if ctype in ("slider", "playback"):
        lo, hi = control.get("min"), control.get("max")
        if not _is_number(value):
            return False, f"{value!r} is not a number"
        if _is_number(lo) and _is_number(hi) and not (lo <= value <= hi):
            return False, f"{value} is outside {lo} to {hi}"
        return True, ""
    if ctype == "range":
        if not (isinstance(value, Sequence) and not isinstance(value, str) and len(value) == 2):
            return False, "a range is [from, to]"
        a, b = value
        if _is_number(a) and _is_number(b) and a >= b:
            return False, "a range must end after it starts"
        if isinstance(a, str) and isinstance(b, str) and a >= b:
            return False, "a range must end after it starts"
        return True, ""
    return True, ""


def select_data(document: Mapping[str, Any], resolved: ResolvedState) -> dict[str, Any]:
    """The data block to draw under *resolved*: the chosen frame, or the base."""
    data = dict(document.get("data") or {})
    frames = data.pop("frames", None)
    if frames:
        items = frames.get("items") or []
        chosen = next((it for it in items if it.get("value") == resolved.frame), items[0] if items else None)
        if chosen is not None:
            merged = dict(data)
            merged.update(chosen.get("data") or {})
            data = merged
    if resolved.groups is not None and "series" in data:
        data["series"] = [s for s in data["series"]
                          if not s.get("group") or s.get("group") in resolved.groups]
    return data


def json_schema() -> dict[str, Any]:
    """A JSON Schema (draft 2020-12) for the document's outer shape.

    Structural only: the cross-field rules (a frame control needs frames, a
    measured chart may not carry a modelled series) are :func:`validate`'s, and
    a document passing the schema but failing :func:`validate` is not a spec.
    """
    axis = {"type": "object", "additionalProperties": False, "properties": {
        "type": {"enum": list(AXIS_TYPES)}, "title": {"type": "string"},
        "unit": {"type": "string"}, "range": {"type": "array", "minItems": 2, "maxItems": 2},
        "side": {"enum": ["left", "right"]}}}
    series = {"type": "object", "required": ["name", "y"], "additionalProperties": False,
              "properties": {k: {} for k in sorted(SERIES_KEYS)}}
    data_block = {"type": "object", "properties": {
        "query": {"type": "object", "required": ["site", "channels"]},
        "verb": {"type": "object", "required": ["name"]},
        "series": {"type": "array", "items": series, "minItems": 1},
        "field": {"type": "object", "required": ["z"]},
        "value": {"type": "object"},
        "tree": {"type": "object", "description": "one of root (nested), folded (stack lines) or rows"},
        "scene": {"type": "object", "description": "one of mesh, points, grid or ref, plus scalars, elements"},
        "frames": {"type": "object", "required": ["key", "items"]}}}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://axiom.b-treelabs.com/schemas/chart-spec/2.0.json",
        "title": "Axiom ChartSpec 2.0",
        "type": "object",
        "required": ["schema_version", "kind", "title", "data"],
        "additionalProperties": False,
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "id": {"type": "string", "pattern": _SLUG.pattern},
            "kind": {"enum": list(_KINDS)},
            "title": {"type": "string", "minLength": 1},
            "subtitle": {"type": "string"},
            "source": {"type": "object", "required": ["type"], "properties": {
                "type": {"enum": ["native", "imported"]},
                "format": {"enum": list(SOURCE_FORMATS)}}},
            "data": data_block,
            "axes": {"type": "object", "additionalProperties": False,
                     "properties": {"x": axis, "y": axis, "y2": axis}},
            "encoding": {"type": "object", "additionalProperties": False,
                         "properties": {k: {} for k in sorted(ENCODING_KEYS)}},
            "marks": {"type": "array", "items": {"type": "object", "required": ["type"],
                      "properties": {"type": {"enum": list(MARK_TYPES)},
                                     "tone": {"enum": list(TONES)}}}},
            "layout": {"type": "object"},
            "controls": {"type": "array", "items": {
                "type": "object", "required": ["id", "type", "label", "bind"],
                "properties": {"type": {"enum": list(CONTROL_TYPES)},
                               "bind": {"type": "object", "required": ["to"],
                                        "properties": {"to": {"enum": list(BIND_TARGETS)}}}}}},
            "catalog": {"type": "object", "additionalProperties": False,
                        "properties": {k: {} for k in sorted(CATALOG_KEYS)}},
            "display_units": {"type": "object", "additionalProperties": {"type": "string"}},
            "series_colours": {"type": "object", "additionalProperties": {"type": "string"}},
            "palette": {"type": "string"},
        },
    }


__all__ = [
    "AXIS_TYPES", "BIND_TARGETS", "CONTROL_TYPES", "DATA_SHAPES", "DOCUMENT_KEYS",
    "Kind", "MARK_TYPES", "ResolvedState", "SCHEMA_VERSION", "UnifiedSpecError",
    "canonical_json", "default_value", "figure_digest", "frames_of", "json_schema",
    "lookup_kind", "parse", "register_kind", "registered_kinds", "resolve_state",
    "select_data", "spec_id", "validate",
]
