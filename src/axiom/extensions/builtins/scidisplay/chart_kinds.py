# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every kind of chart a ChartSpec 2.0 can name, drawn. SVG, no dependencies.

:func:`render_spec` is the one door: a document and a state in, a figure out.
It never reads ``source``, so an imported chart and a native one with the
same content are drawn byte-for-byte alike (the seamlessness test holds this).

A time series goes to :func:`.chart_svg.render_svg`, the renderer the Charts
page already draws with, so a line imported from Plotly is the platform's line
and not a lookalike. Every other kind is drawn here in the same chrome: the
same paper, ink, rules and type sizes, the same serif for numbers and sans for
labels, units stated once on each axis, tokens for everything that themes.

**Accessible by construction.** The root is ``role="img"`` with a ``<title>``
and a ``<desc>`` that says what the chart shows. Every data mark carries
``data-axk-mark``, an ``aria-label`` with its value and unit, and a ``<title>``
for a hover tooltip; up to :data:`FOCUSABLE_MAX` marks are also keyboard
focusable. Past that (a 4,000-cell field) one summary mark takes focus instead,
because tabbing through four thousand cells is not access, it is a trap.
"""

from __future__ import annotations

import html
import math
import statistics
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import chart_marks, palettes
from . import unified_spec as us
from .chart_colour import assign_colours, family_for
from .chart_svg import (
    FONT,
    INK,
    LABEL_FONT,
    MUTED,
    PAPER,
    RULE,
    Geometry,
    Series,
    _fit,
    _nice_ticks,
    _quiet,
    _time_labels,
    _time_ticks,
    render_svg,
    themed,
)
from .numeric_format import format_number
from .units import declared
from .units import label as unit_label

TRACK = themed("--axk-plot-track", "#e5e7eb")
#: Marks past this count get a hover title but not a tab stop; see the module doc.
FOCUSABLE_MAX = 240
#: Past this a field's cells carry no title either; the summary mark speaks for it.
TITLED_MAX = 2600

_PRETTY_UNITS = {"degC": "°C", "degF": "°F", "percent": "%", "pct": "%", "degK": "K"}


@dataclass
class Rendered:
    svg: str
    notes: list[str] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    controls: list[dict[str, Any]] = field(default_factory=list)
    spec_id: str = ""
    readout: dict[str, Any] | None = None
    #: How far the store's readings go, when a resolver said; lets a window
    #: control stop at the edge of the record.
    bounds: dict[str, str] | None = None
    #: A 3D kind's resolved scene, for the browser's WebGL view to draw into
    #: ``scene["box"]`` of the SVG.
    scene: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        out = {"svg": self.svg, "notes": self.notes, "state": self.state,
               "controls": self.controls, "spec_id": self.spec_id}
        if self.readout:
            out["readout"] = self.readout
        if self.bounds:
            out["bounds"] = self.bounds
        if self.scene:
            out["scene"] = self.scene
        return out


#: ``resolve(document, data, state) -> data`` turns a ``query`` or ``verb``
#: block into inline ``series``. It belongs to a figure source, which knows the
#: store; the renderer never does.
Resolver = Callable[[Mapping[str, Any], Mapping[str, Any], us.ResolvedState], Mapping[str, Any]]


# ---------------------------------------------------------------------------
# Small things used everywhere.
# ---------------------------------------------------------------------------


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _pretty(unit: str) -> str:
    return _PRETTY_UNITS.get(unit, unit)


def _num(v: float, span: float | None = None) -> str:
    if abs(v) >= 1e6 or (0 < abs(v) < 1e-3):
        span = None
    if span is not None and span > 0:
        places = max(0, min(4, 2 - int(math.floor(math.log10(span)))))
        return format_number(round(v, places))
    mag = abs(v)
    if mag >= 1e6 or (0 < mag < 1e-3):
        e = int(math.floor(math.log10(mag)))
        return f"{v / 10 ** e:.2f}×10{_sup(e)}"
    if mag >= 1000 or mag == 0:
        return format_number(round(v, 0 if mag >= 100 else 2))
    digits = max(0, 3 - int(math.floor(math.log10(mag))) - 1)
    return format_number(round(v, min(digits, 4)))


def _with_unit(v: float | None, unit: str, span: float | None = None) -> str:
    if v is None:
        return "no reading"
    text = _num(v, span)
    if not declared(unit):
        return f"{text} (no unit)"
    u = _pretty(unit)
    return f"{text}{'' if u in ('%', '°C', '°F') else ' '}{u}"


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _instant(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, str) and len(v) >= 8 and v[:4].isdigit() and v[4] == "-":
        try:
            m = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return m if m.tzinfo else m.replace(tzinfo=UTC)
    return None


def _uid(*parts: Any) -> str:
    return "%08x" % (zlib.crc32("|".join(str(p) for p in parts).encode()) & 0xFFFFFFFF)


class _Marks:
    """Counts marks so the focus budget is applied per figure."""

    def __init__(self, total: int) -> None:
        self.focus = total <= FOCUSABLE_MAX
        self.titled = total <= TITLED_MAX

    def attrs(self, label: str) -> str:
        tab = ' tabindex="0"' if self.focus else ""
        return f' data-axk-mark=""{tab} aria-label="{_esc(label)}"'

    def el(self, tag: str, attrs: str, label: str) -> str:
        if not self.titled:
            return f"<{tag} {attrs} data-axk-mark=\"\"/>"
        return f"<{tag} {attrs}{self.attrs(label)}><title>{_esc(label)}</title></{tag}>"


def _root(geo: Geometry, title: str, desc: str, kind: str) -> list[str]:
    tid = f"axk-t-{_uid(kind, title, geo.width, geo.height)}"
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{geo.width}" height="{geo.height}" '
        f'viewBox="0 0 {geo.width} {geo.height}" font-family="{FONT}" role="img" '
        f'aria-labelledby="{tid} {tid}-d" data-kind="{_esc(kind)}">',
        f'<title id="{tid}">{_esc(title)}</title><desc id="{tid}-d">{_esc(desc)}</desc>',
        f'<rect width="{geo.width}" height="{geo.height}" fill="{PAPER}"/>',
    ]


def _heading(out: list[str], geo: Geometry, title: str, subtitle: str, *, left: float | None = None,
             reserve: float = 0.0) -> float:
    """Title and subtitle exactly as the time-series renderer sets them."""
    x = geo.left if left is None else left
    line, small = 16.5 * geo.scale, 13.5 * geo.scale
    room = geo.width - x - reserve - 8
    y = line
    if title:
        out.append(f'<text class="axk-title" x="{x}" y="{y:.1f}" font-size="{16.5 * geo.scale:.2f}" '
                   f'fill="{INK}" font-family="{LABEL_FONT}" font-weight="600">'
                   f'{_esc(_fit(title, room, 16.5 * geo.scale))}</text>')
        y += small * 1.35
    if subtitle:
        out.append(f'<text x="{x}" y="{y:.1f}" font-size="{12.5 * geo.scale:.2f}" fill="{MUTED}" '
                   f'font-family="{LABEL_FONT}">{_esc(_fit(subtitle, room, 12.5 * geo.scale))}</text>')
        y += small * 1.1
    return y


def _footer(out: list[str], geo: Geometry, notes: list[str], at: float) -> float:
    for i, note in enumerate(notes):
        out.append(f'<text x="{geo.left}" y="{at + 13.5 * i * 1.25:.1f}" font-size="10" fill="{MUTED}" '
                   f'font-family="{LABEL_FONT}" font-style="italic">{_esc(note)}</text>')
    return at + 13.5 * 1.25 * len(notes)


def _finish(out: list[str], geo: Geometry, bottom: float) -> str:
    height = max(geo.height, int(math.ceil(bottom + 6)))
    if height != geo.height:
        out[0] = out[0].replace(f'height="{geo.height}" viewBox="0 0 {geo.width} {geo.height}"',
                                f'height="{height}" viewBox="0 0 {geo.width} {height}"', 1)
        out[2] = out[2].replace(f'height="{geo.height}"', f'height="{height}"', 1)
    out.append("</svg>")
    return "\n".join(out)


def _empty(geo: Geometry, kind: str, title: str, message: str) -> str:
    out = _root(geo, title, message, kind)
    y = _heading(out, geo, title, "")
    out.append(f'<text x="{geo.left}" y="{max(y + 30, geo.height / 2):.1f}" font-size="13" fill="{MUTED}" '
               f'font-family="{LABEL_FONT}" font-style="italic">{_esc(message)}</text>')
    return _finish(out, geo, 0)


def _series_colours(series: Sequence[Mapping[str, Any]], doc: Mapping[str, Any],
                    preferred: Mapping[str, str] | None) -> list[str]:
    """Hue by what each series measures (the platform rule), declared wins."""
    asked = {s["name"]: s["colour"] for s in series if s.get("colour")}
    asked.update(doc.get("series_colours") or {})
    # A count or a share is not a physical quantity with a hue of its own; a
    # declared one takes the categorical palette so its series stay apart. An
    # EMPTY unit keeps the undeclared grey, which is a finding, not a style.
    if all(declared(s.get("unit") or "") and family_for(s.get("unit")) in ("undeclared", "fraction")
           for s in series):
        pal = _category_colours(len(series), doc)
        return [palettes.series_colour(asked.get(s["name"], pal[i])) for i, s in enumerate(series)]
    colours, _ = assign_colours([(s["name"], s.get("unit") or "") for s in series],
                                declared=asked, preferred=preferred)
    by_name = {s["name"]: i for i, s in enumerate(series) if not s.get("modelled")}
    for i, s in enumerate(series):
        if s.get("modelled") and s.get("against") in by_name and not s.get("colour"):
            colours[i] = colours[by_name[s["against"]]]
    return [palettes.series_colour(c) for c in colours]


def _category_colours(n: int, doc: Mapping[str, Any]) -> list[str]:
    pal = palettes.categorical(doc.get("palette"))
    return [palettes.series_colour(pal[i % len(pal)]) for i in range(n)]


# ---------------------------------------------------------------------------
# Scales.
# ---------------------------------------------------------------------------


class _Scale:
    """One axis: a domain, a pixel range, ticks and their labels."""

    def __init__(self, kind: str, lo: Any, hi: Any, a: float, b: float, *,
                 categories: list[str] | None = None) -> None:
        self.kind, self.lo, self.hi, self.a, self.b = kind, lo, hi, a, b
        self.categories = categories or []

    def __call__(self, v: Any) -> float | None:
        if self.kind == "category":
            if v not in self.categories:
                return None
            n = len(self.categories)
            step = (self.b - self.a) / max(n, 1)
            return self.a + step * (self.categories.index(v) + 0.5)
        if self.kind == "time":
            m = _instant(v)
            if m is None:
                return None
            span = (self.hi - self.lo).total_seconds() or 1.0
            return self.a + (self.b - self.a) * ((m - self.lo).total_seconds() / span)
        if not _is_num(v):
            return None
        if self.kind == "log":
            if v <= 0:
                return None
            lo, hi = math.log10(self.lo), math.log10(self.hi)
            return self.a + (self.b - self.a) * ((math.log10(v) - lo) / ((hi - lo) or 1))
        return self.a + (self.b - self.a) * ((v - self.lo) / ((self.hi - self.lo) or 1))

    @property
    def band(self) -> float:
        return abs(self.b - self.a) / max(len(self.categories), 1)

    def ticks(self) -> list[tuple[float, str]]:
        if self.kind == "category":
            return [(self(c), c) for c in self.categories]  # type: ignore[misc]
        if self.kind == "time":
            ticks, step = _time_ticks(self.lo, self.hi)
            labels = _time_labels(ticks, step)
            return [(self(t), lab[0] + (f" {lab[1]}" if lab[1] else "")) for t, lab in zip(ticks, labels, strict=True)]  # type: ignore[misc]
        if self.kind == "log":
            lo, hi = math.floor(math.log10(self.lo) + 1e-9), math.ceil(math.log10(self.hi) - 1e-9)
            out = []
            for e in range(int(lo), int(hi) + 1):
                text = f"10{_sup(e)}" if abs(e) >= 3 else format_number(10.0 ** e)
                out.append((self(10.0 ** e), text))
            return [(p, t) for p, t in out if p is not None]
        span = self.hi - self.lo
        return [(self(t), _num(t, span)) for t in _nice_ticks(self.lo, self.hi)]  # type: ignore[misc]


_SUP = str.maketrans("-0123456789", "⁻⁰¹²³⁴⁵⁶⁷⁸⁹")


def _sup(e: int) -> str:
    return str(e).translate(_SUP)


def _numeric_domain(values: list[float], *, kind: str, zero: bool, fixed: Sequence[Any] | None,
                    pad: float = 0.08) -> tuple[float, float]:
    if fixed and len(fixed) == 2 and _is_num(fixed[0]) and _is_num(fixed[1]):
        return float(fixed[0]), float(fixed[1])
    vals = [v for v in values if _is_num(v) and (kind != "log" or v > 0)]
    if not vals:
        return (1.0, 10.0) if kind == "log" else (0.0, 1.0)
    lo, hi = min(vals), max(vals)
    if kind == "log":
        return 10 ** math.floor(math.log10(lo)), 10 ** math.ceil(math.log10(hi))
    if zero:
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    if lo == hi:
        d = abs(lo) * 0.1 or 1.0
        return lo - d, hi + d
    d = (hi - lo) * pad
    return (lo if zero and lo == 0 else lo - d), (hi if zero and hi == 0 else hi + d)


def _x_kind(doc: Mapping[str, Any], series: Sequence[Mapping[str, Any]]) -> str:
    declared_type = ((doc.get("axes") or {}).get("x") or {}).get("type")
    if declared_type:
        return declared_type
    xs = [x for s in series for x in (s.get("x") or [])]
    if not xs:
        return "linear"
    if all(_instant(x) is not None for x in xs[:50]):
        return "time"
    if all(_is_num(x) for x in xs):
        return "linear"
    return "category"


def _xs(s: Mapping[str, Any]) -> list[Any]:
    return list(s.get("x") or range(len(s.get("y") or [])))


# ---------------------------------------------------------------------------
# The cartesian frame: axes, ticks, gridlines, marks layer, a plot box.
# ---------------------------------------------------------------------------


@dataclass
class _Frame:
    out: list[str]
    geo: Geometry
    box: tuple[float, float, float, float]
    x: _Scale
    y: dict[str, _Scale]
    units: dict[str, str]
    clip: str
    bottom: float


def _frame(doc: Mapping[str, Any], geo: Geometry, *, title: str, subtitle: str, desc: str,
           x_kind: str, x_values: list[Any], y_values: dict[str, list[float]],
           y_units: dict[str, str], zero: bool, right: float, categories: list[str] | None = None,
           horizontal: bool = False, legend: list[tuple[str, str]] | None = None,
           y_kind: dict[str, str] | None = None) -> _Frame:
    """Lay out a plot and draw everything that is not data."""
    axes = doc.get("axes") or {}
    y_kind = y_kind or {}
    out = _root(geo, title, desc, doc["kind"])
    top = _heading(out, geo, title, subtitle)
    if legend:
        top = _legend(out, geo, legend, top)
    small = 13.5 * geo.scale
    top += small * 1.4  # room for the unit above the value axis

    # Value scales first, so the left gutter can be measured from their labels.
    ydom: dict[str, tuple[float, float]] = {}
    for axis, vals in y_values.items():
        kind = y_kind.get(axis) or (axes.get(axis) or {}).get("type") or "linear"
        ydom[axis] = _numeric_domain(vals, kind=kind, zero=zero, fixed=(axes.get(axis) or {}).get("range"))
        y_kind[axis] = kind
    tick_px = 11.5 * geo.scale

    def labels_of(axis: str) -> list[str]:
        lo, hi = ydom[axis]
        return [t for _, t in _Scale(y_kind[axis], lo, hi, 0, 1).ticks()]

    value_axes = list(ydom)
    primary = "y" if "y" in ydom else (value_axes[0] if value_axes else "y")
    if horizontal:
        # Categories down the left: the gutter fits the longest category name.
        longest = max((len(c) for c in categories or []), default=4)
        left = min(max(56.0, longest * tick_px * 0.56 + 18), geo.width * 0.34)
    else:
        widest = max((len(t) for t in labels_of(primary)), default=3) if primary in ydom else 3
        widest = max(widest, len(unit_label(y_units.get(primary, ""), brief=True)))
        left = max(48.0, widest * tick_px * 0.62 + 18)
    y2 = "y2" in ydom
    gutter2 = max((len(t) for t in labels_of("y2")), default=3) * tick_px * 0.62 + 20 if y2 else 0.0
    x_title = ((axes.get("x") or {}).get("title") or "")
    bottom_room = small * 2.1 + (small * 1.3 if x_title else 0)
    plot_l = left
    plot_w = max(60.0, geo.width - left - right - gutter2)
    plot_t = top
    plot_h = max(60.0, geo.height - top - bottom_room)

    # The category / x scale.
    if horizontal:
        xs = _Scale("category", None, None, plot_t, plot_t + plot_h, categories=categories)
        val_a, val_b = plot_l, plot_l + plot_w
    else:
        if x_kind == "category":
            xs = _Scale("category", None, None, plot_l, plot_l + plot_w, categories=categories)
        elif x_kind == "time":
            moments = [m for m in (_instant(v) for v in x_values) if m]
            rng = (axes.get("x") or {}).get("range")
            lo = _instant(rng[0]) if rng else None
            hi = _instant(rng[1]) if rng else None
            lo = lo or (min(moments) if moments else datetime(2000, 1, 1, tzinfo=UTC))
            hi = hi or (max(moments) if moments else lo)
            if hi <= lo:
                hi = datetime.fromtimestamp(lo.timestamp() + 1, UTC)
            xs = _Scale("time", lo, hi, plot_l, plot_l + plot_w)
        else:
            lo, hi = _numeric_domain([v for v in x_values if _is_num(v)], kind=x_kind, zero=False,
                                     fixed=(axes.get("x") or {}).get("range"), pad=0.0)
            xs = _Scale(x_kind, lo, hi, plot_l, plot_l + plot_w)
        val_a, val_b = plot_t + plot_h, plot_t
    yscales = {a: _Scale(y_kind[a], ydom[a][0], ydom[a][1], val_a, val_b) for a in ydom}

    # Gridlines and value ticks.
    if primary in yscales:
        ys = yscales[primary]
        lo_px, hi_px = sorted((ys.a, ys.b))
        for pos, text in ys.ticks():
            if pos is None or not (lo_px - 1 <= pos <= hi_px + 1):
                continue
            if horizontal:
                out.append(f'<line x1="{pos:.1f}" y1="{plot_t:.1f}" x2="{pos:.1f}" y2="{plot_t + plot_h:.1f}" '
                           f'stroke="{RULE}" stroke-width="1"/>')
                out.append(f'<text x="{pos:.1f}" y="{plot_t + plot_h + small * 1.45:.1f}" font-size="{tick_px:.2f}" '
                           f'fill="{MUTED}" text-anchor="middle">{_esc(text)}</text>')
            else:
                out.append(f'<line x1="{plot_l:.1f}" y1="{pos:.1f}" x2="{plot_l + plot_w:.1f}" y2="{pos:.1f}" '
                           f'stroke="{RULE}" stroke-width="1"/>')
                out.append(f'<text x="{plot_l - 10:.1f}" y="{pos + 4:.1f}" font-size="{tick_px:.2f}" '
                           f'fill="{MUTED}" text-anchor="end">{_esc(text)}</text>')
        unit = y_units.get(primary, "")
        ink = MUTED
        quiet = '' if declared(unit) else ' font-style="italic"'
        if horizontal:
            out.append(f'<text x="{plot_l + plot_w:.1f}" y="{plot_t + plot_h + small * 2.75:.1f}" font-size="11" '
                       f'fill="{ink}"{quiet} text-anchor="end" font-family="{LABEL_FONT}" letter-spacing="0.05em">'
                       f'{_esc(unit_label(unit, brief=True))}</text>')
        else:
            out.append(f'<text x="{plot_l - 10:.1f}" y="{plot_t - 9:.1f}" font-size="11" fill="{ink}"{quiet} '
                       f'text-anchor="end" font-family="{LABEL_FONT}" letter-spacing="0.05em">'
                       f'{_esc(unit_label(unit, brief=True))}</text>')
    if y2 and not horizontal:
        ys = yscales["y2"]
        rx = plot_l + plot_w
        out.append(f'<line x1="{rx:.1f}" y1="{plot_t:.1f}" x2="{rx:.1f}" y2="{plot_t + plot_h:.1f}" '
                   f'stroke="{INK}" stroke-width="1.1"/>')
        for pos, text in ys.ticks():
            if pos is None or not (plot_t - 1 <= pos <= plot_t + plot_h + 1):
                continue
            out.append(f'<line x1="{rx:.1f}" y1="{pos:.1f}" x2="{rx + 4:.1f}" y2="{pos:.1f}" stroke="{INK}" stroke-width="1"/>')
            out.append(f'<text x="{rx + 8:.1f}" y="{pos + 4:.1f}" font-size="{tick_px:.2f}" fill="{MUTED}" '
                       f'text-anchor="start">{_esc(text)}</text>')
        unit = y_units.get("y2", "")
        out.append(f'<text x="{rx + 8:.1f}" y="{plot_t - 9:.1f}" font-size="11" '
                   f'fill="{MUTED}"{_quiet(unit)} text-anchor="start" '
                   f'font-family="{LABEL_FONT}" letter-spacing="0.05em">{_esc(unit_label(unit, brief=True))}</text>')

    # The category / x axis.
    if horizontal:
        for pos, text in xs.ticks():
            out.append(f'<text x="{plot_l - 8:.1f}" y="{pos + 4:.1f}" font-size="{tick_px:.2f}" fill="{MUTED}" '
                       f'text-anchor="end" font-family="{LABEL_FONT}">'
                       f'{_esc(_fit(text, plot_l - 14, tick_px))}</text>')
        out.append(f'<line x1="{plot_l:.1f}" y1="{plot_t:.1f}" x2="{plot_l:.1f}" y2="{plot_t + plot_h:.1f}" '
                   f'stroke="{INK}" stroke-width="1.1"/>')
    else:
        ticks = [(p, t) for p, t in xs.ticks() if p is not None and plot_l - 1 <= p <= plot_l + plot_w + 1]
        need = max((len(t) for _, t in ticks), default=4) * tick_px * 0.62 + tick_px * 1.6
        kept: list[tuple[float, str]] = []
        for p, t in ticks:
            if not kept or p - kept[-1][0] >= need:
                kept.append((p, t))
        out.append('<g class="axk-xticks">')
        for p, t in kept:
            anchor = "middle"
            if xs.kind != "category":
                anchor = "start" if p <= plot_l + 1 else ("end" if p >= plot_l + plot_w - 1 else "middle")
            out.append(f'<text x="{p:.1f}" y="{plot_t + plot_h + small * 1.45:.1f}" font-size="{tick_px:.2f}" '
                       f'fill="{MUTED}" text-anchor="{anchor}">{_esc(t)}</text>')
        out.append("</g>")
        out.append(f'<line x1="{plot_l:.1f}" y1="{plot_t + plot_h:.1f}" x2="{plot_l + plot_w:.1f}" '
                   f'y2="{plot_t + plot_h:.1f}" stroke="{INK}" stroke-width="1.1"/>')
    bottom = plot_t + plot_h + small * 2.1
    if x_title:
        out.append(f'<text x="{plot_l + plot_w / 2:.1f}" y="{bottom + small * 0.6:.1f}" font-size="11.5" '
                   f'fill="{MUTED}" text-anchor="middle" font-family="{LABEL_FONT}">'
                   f'{_esc(chart_marks.strip_html(x_title))}</text>')
        bottom += small * 1.3
    clip = f"axk-plot-{_uid(plot_l, plot_t, plot_w, plot_h, doc['kind'])}"
    out.append(f'<clipPath id="{clip}"><path d="M{plot_l:.1f},{plot_t:.1f} h{plot_w:.1f} v{plot_h:.1f} '
               f'h-{plot_w:.1f} Z"/></clipPath>')
    return _Frame(out, geo, (plot_l, plot_t, plot_w, plot_h), xs, yscales, y_units, clip, bottom)


def _legend(out: list[str], geo: Geometry, items: list[tuple[str, str]], top: float) -> float:
    """A one-line key under the heading, for marks a direct label cannot reach."""
    x = float(geo.left)
    y = top + 2
    for name, colour in items:
        w = len(name) * 11.5 * 0.56 + 26
        if x + w > geo.width - 12:
            x = float(geo.left)
            y += 16
        out.append(f'<rect x="{x:.1f}" y="{y - 9:.1f}" width="11" height="11" rx="2" fill="{colour}"/>')
        out.append(f'<text x="{x + 16:.1f}" y="{y:.1f}" font-size="11.5" fill="{INK}" '
                   f'font-family="{LABEL_FONT}">{_esc(name)}</text>')
        x += w
    return y + 8


def _marks_layer(fr: _Frame, doc: Mapping[str, Any], hidden: set[str], panel: str | None,
                 *, over: bool) -> None:
    marks = chart_marks.visible(doc.get("marks"), hidden, panel)
    if not marks:
        return

    def y_of(v: Any, axis: str) -> float | None:
        sc = fr.y.get(axis)
        return sc(v) if sc else None

    fn = chart_marks.draw_over if over else chart_marks.draw_under
    kwargs: dict[str, Any] = {"box": fr.box, "x_of": fr.x, "y_of": y_of, "label_font": LABEL_FONT}
    if over:
        kwargs["ink"] = INK
    fr.out.extend(fn(marks, **kwargs))


def _direct_labels(fr: _Frame, items: list[tuple[float, str, str, str]]) -> None:
    """Name each line at its end, nudged apart so no two collide."""
    left, top, width, height = fr.box
    gap = 13.5 * 1.7
    items = sorted(items)
    placed: list[float] = []
    for want, *_ in items:
        placed.append(want if not placed else max(want, placed[-1] + gap))
    overflow = (placed[-1] - (top + height)) if placed else 0
    if overflow > 0:
        placed = [p - overflow for p in placed]
    for y, (want, name, colour, reading) in zip(placed, items, strict=True):
        x = left + width + 9
        fr.out.append(f'<line x1="{left + width:.1f}" y1="{want:.1f}" x2="{x - 2:.1f}" y2="{y:.1f}" '
                      f'stroke="{colour}" stroke-width="1" opacity="0.5"/>')
        fr.out.append(f'<text x="{x:.1f}" y="{y + 3.5:.1f}" font-size="12" fill="{colour}" '
                      f'font-family="{LABEL_FONT}" font-weight="600">{_esc(name)}'
                      f'<tspan fill="{MUTED}" font-weight="400" dx="4.5" font-size="10.5">{_esc(reading)}</tspan></text>')


# ---------------------------------------------------------------------------
# Kinds on axes.
# ---------------------------------------------------------------------------


@dataclass
class _Ctx:
    doc: Mapping[str, Any]
    data: Mapping[str, Any]
    state: us.ResolvedState
    geo: Geometry
    notes: list[str]
    preferred: Mapping[str, str] | None = None
    panel: str | None = None
    readout: dict[str, Any] | None = None
    scene: dict[str, Any] | None = None

    @property
    def title(self) -> str:
        return self.doc.get("title", "")

    @property
    def subtitle(self) -> str:
        return self.doc.get("subtitle", "")

    def series(self) -> list[dict[str, Any]]:
        out = []
        for s in self.data.get("series") or []:
            if self.panel is not None and s.get("panel") not in (None, self.panel):
                continue
            out.append(dict(s))
        return out


def _line_label(s: Mapping[str, Any]) -> str:
    return s["name"] + (" (model)" if s.get("modelled") else "")


def _xy_points(s: Mapping[str, Any], x_kind: str) -> list[tuple[Any, float | None]]:
    ys = s.get("y") or []
    return list(zip(_xs(s), [v if _is_num(v) else None for v in ys], strict=False))


def _draw_lines(ctx: _Ctx) -> str:
    """line / step / area, on any x axis that is not time-only-line."""
    doc, kind = ctx.doc, ctx.doc["kind"]
    series = ctx.series()
    if not series:
        return _empty(ctx.geo, kind, ctx.title, "no series to draw")
    x_kind = _x_kind(doc, series)
    enc = doc.get("encoding") or {}
    stack = enc.get("stack", "none") if kind == "area" else "none"
    step = kind == "step" or enc.get("curve") == "step"
    colours = _series_colours(series, doc, ctx.preferred)
    categories = None
    if x_kind == "category":
        categories = list(dict.fromkeys(str(x) for s in series for x in _xs(s)))
    # Stacking sums along x, per axis.
    stacked: list[list[tuple[Any, float | None, float]]] = []
    base: dict[Any, float] = {}
    totals: dict[Any, float] = {}
    if stack == "percent":
        for s in series:
            for x, v in _xy_points(s, x_kind):
                if v is not None:
                    totals[x] = totals.get(x, 0.0) + v
    for s in series:
        pts = []
        for x, v in _xy_points(s, x_kind):
            if stack == "none":
                pts.append((x, v, 0.0))
                continue
            if v is not None and stack == "percent":
                v = 100.0 * v / (totals.get(x) or 1.0)
            lo = base.get(x, 0.0)
            hi = lo + (v or 0.0)
            base[x] = hi
            pts.append((x, None if v is None else hi, lo))
        stacked.append(pts)
    y_values: dict[str, list[float]] = {}
    y_units: dict[str, str] = {}
    for s, pts in zip(series, stacked, strict=True):
        axis = s.get("axis", "y")
        y_values.setdefault(axis, []).extend(v for _, v, _ in pts if v is not None)
        if stack != "none":
            y_values[axis].extend(lo for _, _, lo in pts)
        y_units.setdefault(axis, "%" if stack == "percent" else (s.get("unit") or ""))
    zero = kind == "area" and (doc.get("axes") or {}).get("y", {}).get("type") != "log"
    right = min(max(96, max(len(_line_label(s)) for s in series) * 7.1 + 96), ctx.geo.width * 0.36)
    desc = f"{kind} chart of {', '.join(s['name'] for s in series)}"
    fr = _frame(doc, ctx.geo, title=ctx.title, subtitle=ctx.subtitle, desc=desc, x_kind=x_kind,
                x_values=[x for s in series for x in _xs(s)], y_values=y_values, y_units=y_units,
                zero=zero, right=right, categories=categories)
    _marks_layer(fr, doc, ctx.state.hidden_layers, ctx.panel, over=False)
    left, top, width, height = fr.box
    measured = any(not s.get("modelled") for s in series)
    labels: list[tuple[float, str, str, str]] = []
    n_marks = sum(1 for s in series)
    marks = _Marks(n_marks)
    fr.out.append(f'<g class="axk-marks" clip-path="url(#{fr.clip})">')
    for s, pts, colour in zip(series, stacked, colours, strict=True):
        ys = fr.y[s.get("axis", "y")]
        dash = ' stroke-dasharray="5 3.5"' if s.get("modelled") and measured else ""
        runs: list[list[tuple[float, float, float]]] = [[]]
        for x, v, lo in pts:
            px = fr.x(x if x_kind != "category" else str(x))
            py = ys(v) if v is not None else None
            if px is None or py is None:
                if runs[-1]:
                    runs.append([])
                continue
            pb = ys(lo) if stack != "none" else (ys.a if zero else None)
            if pb is None:
                pb = top + height
            runs[-1].append((px, py, pb))
        for run in runs:
            if not run:
                continue
            path: list[str] = []
            for i, (px, py, _) in enumerate(run):
                if i == 0:
                    path.append(f"M{px:.1f},{py:.1f}")
                elif step:
                    path.append(f"H{px:.1f}V{py:.1f}")
                else:
                    path.append(f"L{px:.1f},{py:.1f}")
            if kind == "area":
                back = []
                for i, (px, _, pb) in enumerate(reversed(run)):
                    back.append(f"L{px:.1f},{pb:.1f}" if not step or i == 0 else f"V{pb:.1f}H{px:.1f}")
                fill = "".join(path) + "".join(back) + "Z"
                fr.out.append(f'<path d="{fill}" fill="{colour}" fill-opacity="{0.18 if stack == "none" else 0.55}" stroke="none"/>')
            fr.out.append(f'<path d="{"".join(path)}" fill="none" stroke="{colour}" stroke-width="1.6" '
                          f'stroke-linejoin="round" stroke-linecap="round"{dash}/>')
        live = [(x, v) for x, v, _ in pts if v is not None]
        if live:
            last_x, last_v = live[-1]
            raw = [v for v in (s.get("y") or []) if _is_num(v)]
            span = (max(raw) - min(raw)) if raw else None
            reading = _with_unit(raw[-1] if raw else last_v, s.get("unit") or "", span)
            py = ys(last_v)
            if py is not None:
                labels.append((py, _line_label(s), colour, reading))
            lo_v, hi_v = (min(raw), max(raw)) if raw else (last_v, last_v)
            fr.out.append(marks.el("g", f'class="axk-series" data-series="{_esc(s["name"])}"',
                                   f"{_line_label(s)}: last {reading}; range "
                                   f"{_with_unit(lo_v, s.get('unit') or '', span)} to "
                                   f"{_with_unit(hi_v, s.get('unit') or '', span)}"))
    fr.out.append("</g>")
    _marks_layer(fr, doc, ctx.state.hidden_layers, ctx.panel, over=True)
    _direct_labels(fr, labels)
    return _finish(fr.out, ctx.geo, _footer(fr.out, ctx.geo, ctx.notes, fr.bottom + 4))


def _draw_time_line(ctx: _Ctx) -> str:
    """A line over time: the platform's own time-series renderer, unchanged."""
    doc = ctx.doc
    series = ctx.series()
    if not series:
        return _empty(ctx.geo, "line", ctx.title, "no series to draw")
    rendered: list[Series] = []
    for s in series:
        points = []
        for x, v in zip(_xs(s), s.get("y") or [], strict=False):
            m = _instant(x)
            if m is not None:
                points.append((m, v if _is_num(v) else None))
        rendered.append(Series(name=s["name"], points=points, unit=s.get("unit") or "",
                               modelled=bool(s.get("modelled")), colour=s.get("colour") or "",
                               against=s.get("against") or "",
                               mark="scatter" if (doc.get("encoding") or {}).get("show_points") else "line"))
    # A model is drawn against the one measurement that shares its unit.
    for r in rendered:
        if r.modelled and not r.against:
            partners = [o for o in rendered if not o.modelled and o.unit == r.unit]
            if len(partners) == 1:
                r.against = partners[0].name
    axes = doc.get("axes") or {}
    secondary = next((s.get("unit") or "" for s in series if s.get("axis") == "y2"), "")
    log_units = tuple(u for axis, u in (("y", next((s.get("unit") or "" for s in series if s.get("axis", "y") == "y"), "")),
                                         ("y2", secondary))
                      if (axes.get(axis) or {}).get("type") == "log" and u)
    window = None
    rng = (axes.get("x") or {}).get("range")
    if ctx.state.window:
        rng = list(ctx.state.window)
    if rng:
        a, b = _instant(rng[0]), _instant(rng[1])
        if a and b and a < b:
            window = (a, b)
    svg = render_svg(
        rendered, title=ctx.title, subtitle=ctx.subtitle, geometry=ctx.geo,
        secondary_unit=secondary, log_units=log_units, display_units=doc.get("display_units"),
        series_colours=doc.get("series_colours"), preferred_colours=ctx.preferred, window=window,
        marks=chart_marks.visible(doc.get("marks"), ctx.state.hidden_layers, ctx.panel),
    )
    desc = f"line chart over time of {', '.join(s['name'] for s in series)}"
    tid = f"axk-t-{_uid('line', ctx.title, ctx.geo.width, ctx.geo.height)}"
    head, _, rest = svg.partition(">")
    svg = (f'{head} role="img" aria-labelledby="{tid} {tid}-d" data-kind="line">'
           f'<title id="{tid}">{_esc(ctx.title)}</title><desc id="{tid}-d">{_esc(desc)}</desc>{rest}')
    # One focusable summary per line: its last reading and its range, so a
    # keyboard reaches what the crosshair shows a pointer.
    marks = _Marks(len(rendered))
    summaries = []
    for r in rendered:
        vals = [v for _, v in r.points if v is not None]
        if not vals:
            continue
        span = max(vals) - min(vals)
        summaries.append(marks.el("g", f'class="axk-series-summary" data-series="{_esc(r.name)}"',
                                  f"{r.name}{' (model)' if r.modelled else ''}: last {_with_unit(vals[-1], r.unit, span)}; "
                                  f"range {_with_unit(min(vals), r.unit, span)} to {_with_unit(max(vals), r.unit, span)}"))
    svg = svg[: svg.rfind("</svg>")] + "".join(summaries) + "</svg>"
    stamps = [m for r in rendered for m, v in r.points if v is not None]
    ctx.readout = {"from": (window[0] if window else min(stamps)).isoformat() if stamps else "",
                   "to": (window[1] if window else max(stamps)).isoformat() if stamps else "",
                   "series": [{"name": r.name, "unit": r.unit,
                               "points": [[int(m.timestamp() * 1000), v] for m, v in r.points]}
                              for r in rendered]}
    return svg


def _bar_values(ctx: _Ctx, series: list[dict[str, Any]]) -> tuple[list[str], list[list[float | None]]]:
    cats: list[str] = []
    for s in series:
        for x in _xs(s):
            label = _cat_label(x)
            if label not in cats:
                cats.append(label)
    table = []
    for s in series:
        row: dict[str, float | None] = {}
        for x, v in zip(_xs(s), s.get("y") or [], strict=False):
            row[_cat_label(x)] = v if _is_num(v) else None
        table.append([row.get(c) for c in cats])
    return cats, table


def _cat_label(x: Any) -> str:
    m = _instant(x)
    if m is not None and isinstance(x, str) and len(x) <= 10:
        return m.strftime("%b %-d")
    if m is not None:
        return m.strftime("%b %-d %H:%M")
    return str(x)


def _draw_bar(ctx: _Ctx) -> str:
    doc = ctx.doc
    series = ctx.series()
    if not series:
        return _empty(ctx.geo, "bar", ctx.title, "no series to draw")
    enc = doc.get("encoding") or {}
    stack = enc.get("stack", "none")
    horizontal = enc.get("orientation") == "horizontal"
    cats, table = _bar_values(ctx, series)
    if stack == "percent":
        totals = [sum(r[i] or 0.0 for r in table) or 1.0 for i in range(len(cats))]
        table = [[None if v is None else 100.0 * v / totals[i] for i, v in enumerate(r)] for r in table]
    multi = len(series) > 1
    colours = (_series_colours(series, doc, ctx.preferred) if multi
               else _series_colours(series, doc, ctx.preferred))
    if not multi and doc.get("palette"):
        colours = [palettes.series_colour(palettes.categorical(doc.get("palette"))[0])]
    if stack != "none":
        tops = [sum(max(r[i] or 0.0, 0.0) for r in table) for i in range(len(cats))]
        vals = tops + [0.0]
    else:
        vals = [v for r in table for v in r if v is not None] + [0.0]
    unit = "%" if stack == "percent" else (series[0].get("unit") or "")
    legend = [(s["name"], c) for s, c in zip(series, colours, strict=True)] if multi else None
    desc = f"bar chart of {', '.join(s['name'] for s in series)} by category"
    fr = _frame(doc, ctx.geo, title=ctx.title, subtitle=ctx.subtitle, desc=desc, x_kind="category",
                x_values=cats, y_values={"y": vals}, y_units={"y": unit}, zero=True, right=24,
                categories=cats, horizontal=horizontal, legend=legend)
    _marks_layer(fr, doc, ctx.state.hidden_layers, ctx.panel, over=False)
    ys = fr.y["y"]
    band = fr.x.band
    inner = band * 0.72
    marks = _Marks(len(cats) * len(series))
    show_values = enc.get("labels", len(cats) * (1 if stack != "none" else len(series)) <= 16)
    fr.out.append(f'<g class="axk-marks" clip-path="url(#{fr.clip})">')
    base = [0.0] * len(cats)
    labels_out: list[str] = []
    for si, (s, row, colour) in enumerate(zip(series, table, colours, strict=True)):
        for ci, v in enumerate(row):
            if v is None:
                continue
            centre = fr.x(cats[ci])
            if centre is None:
                continue
            if stack == "none":
                w = inner / len(series)
                off = (si - (len(series) - 1) / 2) * w
                lo, hi = 0.0, v
            else:
                w, off = inner, 0.0
                lo, hi = base[ci], base[ci] + v
                base[ci] = hi
            a, b = ys(lo), ys(hi)
            if a is None or b is None:
                continue
            shown = _with_unit(v, unit if stack == "percent" else (s.get("unit") or ""))
            raw_v = None
            if stack == "percent":
                orig = [rv for rv in (s.get("y") or [])]
                raw_v = orig[ci] if ci < len(orig) else None
            label = f"{s['name']}, {cats[ci]}: {shown}" + (
                f" ({_with_unit(raw_v, s.get('unit') or '')})" if raw_v is not None else "")
            if horizontal:
                x0, x1 = sorted((a, b))
                attrs = (f'x="{x0:.1f}" y="{centre + off - w / 2:.1f}" width="{x1 - x0:.1f}" '
                         f'height="{w * 0.94:.1f}" fill="{colour}" fill-opacity="0.85"')
            else:
                y0, y1 = sorted((a, b))
                attrs = (f'x="{centre + off - w / 2:.1f}" y="{y0:.1f}" width="{w * 0.94:.1f}" '
                         f'height="{y1 - y0:.1f}" fill="{colour}" fill-opacity="0.85"')
            fr.out.append(marks.el("rect", attrs, label))
            if show_values and stack == "none":
                if horizontal:
                    labels_out.append(f'<text x="{max(a, b) + 4:.1f}" y="{centre + off + 4:.1f}" font-size="10.5" '
                                      f'fill="{MUTED}" font-family="{LABEL_FONT}">{_esc(_num(v))}</text>')
                else:
                    labels_out.append(f'<text x="{centre + off:.1f}" y="{min(a, b) - 4:.1f}" font-size="10.5" '
                                      f'fill="{MUTED}" text-anchor="middle" font-family="{LABEL_FONT}">{_esc(_num(v))}</text>')
    fr.out.append("</g>")
    fr.out.extend(labels_out)
    # A category with nothing in it is said, not left as an empty band that
    # reads as a drawing fault.
    raw_totals = [sum(abs(r[i] or 0.0) for r in table) for i in range(len(cats))]
    for ci, cat in enumerate(cats):
        if raw_totals[ci] == 0:
            centre = fr.x(cat)
            text = "no values in this view"
            if horizontal:
                fr.out.append(f'<text x="{fr.box[0] + 8:.1f}" y="{centre + 4:.1f}" font-size="11" fill="{MUTED}" '
                              f'font-style="italic" font-family="{LABEL_FONT}">{text}</text>')
            else:
                fr.out.append(f'<text x="{centre:.1f}" y="{fr.box[1] + fr.box[3] - 6:.1f}" font-size="10.5" '
                              f'fill="{MUTED}" font-style="italic" text-anchor="middle" '
                              f'font-family="{LABEL_FONT}">none</text>')
    _marks_layer(fr, doc, ctx.state.hidden_layers, ctx.panel, over=True)
    return _finish(fr.out, ctx.geo, _footer(fr.out, ctx.geo, ctx.notes, fr.bottom + 4))


def _bins(values: list[float], asked: int | None) -> list[float]:
    lo, hi = min(values), max(values)
    if lo == hi:
        return [lo - 0.5, hi + 0.5]
    if asked:
        n = asked
    else:
        q = statistics.quantiles(values, n=4) if len(values) >= 4 else [lo, hi]
        iqr = (q[-1] - q[0]) or (hi - lo) / 4
        width = 2 * iqr / (len(values) ** (1 / 3))
        n = int(max(5, min(60, math.ceil((hi - lo) / width)))) if width > 0 else 10
    step = (hi - lo) / n
    return [lo + step * i for i in range(n)] + [hi]


def _draw_histogram(ctx: _Ctx) -> str:
    doc = ctx.doc
    series = ctx.series()
    values_by = [[v for v in (s.get("y") or []) if _is_num(v)] for s in series]
    pooled = [v for vs in values_by for v in vs]
    if not pooled:
        return _empty(ctx.geo, "histogram", ctx.title, "no readings to bin")
    edges = _bins(pooled, (doc.get("encoding") or {}).get("bins"))
    counts = []
    for vs in values_by:
        c = [0] * (len(edges) - 1)
        for v in vs:
            i = min(len(c) - 1, max(0, int((v - edges[0]) / ((edges[-1] - edges[0]) or 1) * len(c))))
            c[i] += 1
        counts.append(c)
    colours = _series_colours(series, doc, ctx.preferred)
    unit = series[0].get("unit") or ""
    doc2 = dict(doc)
    axes = dict(doc.get("axes") or {})
    axes.setdefault("x", {})
    doc2["axes"] = axes
    legend = [(s["name"], c) for s, c in zip(series, colours, strict=True)] if len(series) > 1 else None
    desc = f"histogram of {', '.join(s['name'] for s in series)}, {len(pooled)} readings"
    fr = _frame(doc2, ctx.geo, title=ctx.title, subtitle=ctx.subtitle, desc=desc, x_kind="linear",
                x_values=[edges[0], edges[-1]], y_values={"y": [0.0] + [n for c in counts for n in c]},
                y_units={"y": "readings"}, zero=True, right=24, legend=legend)
    _marks_layer(fr, doc, ctx.state.hidden_layers, ctx.panel, over=False)
    ys = fr.y["y"]
    marks = _Marks(sum(len(c) for c in counts))
    span = edges[-1] - edges[0]
    fr.out.append(f'<g class="axk-marks" clip-path="url(#{fr.clip})">')
    for s, c, colour in zip(series, counts, colours, strict=True):
        for i, n in enumerate(c):
            if n == 0:
                continue
            x0, x1 = fr.x(edges[i]), fr.x(edges[i + 1])
            y0, y1 = ys(n), ys(0)
            if None in (x0, x1, y0, y1):
                continue
            label = (f"{s['name']}: {n} reading{'s' if n != 1 else ''} from "
                     f"{_with_unit(edges[i], unit, span)} to {_with_unit(edges[i + 1], unit, span)}")
            fr.out.append(marks.el("rect", f'x="{x0 + 0.5:.1f}" y="{y0:.1f}" width="{max(0.5, x1 - x0 - 1):.1f}" '
                                   f'height="{y1 - y0:.1f}" fill="{colour}" '
                                   f'fill-opacity="{0.85 if len(series) == 1 else 0.5}"', label))
    fr.out.append("</g>")
    # The value's unit, stated under the axis it is drawn along.
    fr.out.append(f'<text x="{fr.box[0] + fr.box[2]:.1f}" y="{fr.box[1] + fr.box[3] + 13.5 * 2.75:.1f}" '
                  f'font-size="11" fill="{MUTED}" text-anchor="end" font-family="{LABEL_FONT}" '
                  f'letter-spacing="0.05em">{_esc(unit_label(unit, brief=True))}</text>')
    _marks_layer(fr, doc, ctx.state.hidden_layers, ctx.panel, over=True)
    return _finish(fr.out, ctx.geo, _footer(fr.out, ctx.geo, ctx.notes, fr.bottom + 16))


def _quartiles(vs: list[float]) -> tuple[float, float, float, float, float, list[float]]:
    vs = sorted(vs)
    q1, med, q3 = statistics.quantiles(vs, n=4, method="inclusive") if len(vs) > 1 else (vs[0],) * 3
    iqr = q3 - q1
    lo_f, hi_f = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    inside = [v for v in vs if lo_f <= v <= hi_f] or vs
    outliers = [v for v in vs if v < lo_f or v > hi_f]
    return min(inside), q1, med, q3, max(inside), outliers


def _draw_box(ctx: _Ctx) -> str:
    doc = ctx.doc
    series = [s for s in ctx.series() if any(_is_num(v) for v in s.get("y") or [])]
    if not series:
        return _empty(ctx.geo, "box", ctx.title, "no readings to summarise")
    cats = [s["name"] for s in series]
    stats = [_quartiles([v for v in s["y"] if _is_num(v)]) for s in series]
    unit = series[0].get("unit") or ""
    colours = _series_colours(series, doc, ctx.preferred)
    vals = [v for st in stats for v in (st[0], st[4], *st[5])]
    desc = f"box plot of {', '.join(cats)}"
    fr = _frame(doc, ctx.geo, title=ctx.title, subtitle=ctx.subtitle, desc=desc, x_kind="category",
                x_values=cats, y_values={"y": vals}, y_units={"y": unit}, zero=False, right=24,
                categories=cats)
    _marks_layer(fr, doc, ctx.state.hidden_layers, ctx.panel, over=False)
    ys = fr.y["y"]
    span = (max(vals) - min(vals)) if vals else None
    marks = _Marks(len(series) + sum(len(st[5]) for st in stats))
    w = min(fr.x.band * 0.5, 90)
    fr.out.append(f'<g class="axk-marks" clip-path="url(#{fr.clip})">')
    for s, st, colour in zip(series, stats, colours, strict=True):
        cx = fr.x(s["name"])
        lo, q1, med, q3, hi, outliers = st
        p = {k: ys(v) for k, v in (("lo", lo), ("q1", q1), ("med", med), ("q3", q3), ("hi", hi))}
        n = sum(1 for v in s["y"] if _is_num(v))
        label = (f"{s['name']}: median {_with_unit(med, unit, span)}, quartiles "
                 f"{_with_unit(q1, unit, span)} to {_with_unit(q3, unit, span)}, range "
                 f"{_with_unit(lo, unit, span)} to {_with_unit(hi, unit, span)}, {n} readings")
        fr.out.append(f'<line x1="{cx:.1f}" y1="{p["lo"]:.1f}" x2="{cx:.1f}" y2="{p["q1"]:.1f}" stroke="{colour}" stroke-width="1.4"/>')
        fr.out.append(f'<line x1="{cx:.1f}" y1="{p["q3"]:.1f}" x2="{cx:.1f}" y2="{p["hi"]:.1f}" stroke="{colour}" stroke-width="1.4"/>')
        for k in ("lo", "hi"):
            fr.out.append(f'<line x1="{cx - w / 4:.1f}" y1="{p[k]:.1f}" x2="{cx + w / 4:.1f}" y2="{p[k]:.1f}" stroke="{colour}" stroke-width="1.4"/>')
        fr.out.append(marks.el("rect", f'x="{cx - w / 2:.1f}" y="{p["q3"]:.1f}" width="{w:.1f}" '
                               f'height="{max(1.0, p["q1"] - p["q3"]):.1f}" fill="{colour}" fill-opacity="0.22" '
                               f'stroke="{colour}" stroke-width="1.4"', label))
        fr.out.append(f'<line x1="{cx - w / 2:.1f}" y1="{p["med"]:.1f}" x2="{cx + w / 2:.1f}" y2="{p["med"]:.1f}" '
                      f'stroke="{colour}" stroke-width="2.4"/>')
        for v in outliers[:400]:
            py = ys(v)
            fr.out.append(marks.el("circle", f'cx="{cx:.1f}" cy="{py:.1f}" r="2.4" fill="none" stroke="{colour}"',
                                   f"{s['name']}: outlier {_with_unit(v, unit, span)}"))
    fr.out.append("</g>")
    _marks_layer(fr, doc, ctx.state.hidden_layers, ctx.panel, over=True)
    return _finish(fr.out, ctx.geo, _footer(fr.out, ctx.geo, ctx.notes, fr.bottom + 4))


def _fit_line(xs: list[float], ys: list[float]) -> tuple[float, float, float] | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    icpt = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys) or 1.0
    ss_res = sum((y - (slope * x + icpt)) ** 2 for x, y in zip(xs, ys, strict=True))
    return slope, icpt, 1 - ss_res / ss_tot


def _draw_scatter(ctx: _Ctx) -> str:
    doc = ctx.doc
    series = ctx.series()
    pairs = [[(x, y) for x, y in zip(_xs(s), s.get("y") or [], strict=False) if _is_num(x) and _is_num(y)]
             for s in series]
    if not any(pairs):
        return _empty(ctx.geo, "scatter", ctx.title, "no pairs to plot")
    colours = _series_colours(series, doc, ctx.preferred)
    axes = doc.get("axes") or {}
    x_kind = (axes.get("x") or {}).get("type") or "linear"
    legend = [(s["name"], c) for s, c in zip(series, colours, strict=True)] if len(series) > 1 else None
    desc = f"scatter of {', '.join(s['name'] for s in series)}"
    fr = _frame(doc, ctx.geo, title=ctx.title, subtitle=ctx.subtitle, desc=desc, x_kind=x_kind,
                x_values=[x for p in pairs for x, _ in p], y_values={"y": [y for p in pairs for _, y in p]},
                y_units={"y": series[0].get("unit") or ""}, zero=False, right=24, legend=legend)
    _marks_layer(fr, doc, ctx.state.hidden_layers, ctx.panel, over=False)
    ys = fr.y["y"]
    total = sum(len(p) for p in pairs)
    marks = _Marks(total)
    x_unit = (axes.get("x") or {}).get("unit") or ""
    y_unit = series[0].get("unit") or ""
    fr.out.append(f'<g class="axk-marks" clip-path="url(#{fr.clip})">')
    fit_text = []
    for s, p, colour in zip(series, pairs, colours, strict=True):
        r = 2.6 if len(p) < 600 else 1.8
        for x, y in p:
            px, py = fr.x(x), ys(y)
            if px is None or py is None:
                continue
            fr.out.append(marks.el("circle", f'cx="{px:.1f}" cy="{py:.1f}" r="{r}" fill="{colour}" fill-opacity="0.55"',
                                   f"{s['name']}: {_with_unit(x, x_unit)}, {_with_unit(y, y_unit)}"))
        if (doc.get("encoding") or {}).get("fit") == "linear":
            fit = _fit_line([x for x, _ in p], [y for _, y in p])
            if fit:
                slope, icpt, r2 = fit
                lo, hi = fr.x.lo, fr.x.hi
                a, b = (fr.x(lo), ys(slope * lo + icpt)), (fr.x(hi), ys(slope * hi + icpt))
                if None not in (*a, *b):
                    fr.out.append(f'<line x1="{a[0]:.1f}" y1="{a[1]:.1f}" x2="{b[0]:.1f}" y2="{b[1]:.1f}" '
                                  f'stroke="{INK}" stroke-width="1.4" stroke-dasharray="6 3"/>')
                fit_text.append(f"{s['name']}: least-squares fit, slope {_num(slope)} {_esc(y_unit)} per "
                                f"{_esc(x_unit or 'unit')}, R² {r2:.3f}")
    fr.out.append("</g>")
    left, top, width, _ = fr.box
    for i, t in enumerate(fit_text):
        fr.out.append(f'<text x="{left + 8:.1f}" y="{top + 14 + i * 15:.1f}" font-size="11" fill="{INK}" '
                      f'font-family="{LABEL_FONT}">{t}</text>')
    if x_unit and not (axes.get("x") or {}).get("title"):
        fr.out.append(f'<text x="{left + width:.1f}" y="{fr.box[1] + fr.box[3] + 13.5 * 2.75:.1f}" font-size="11" '
                      f'fill="{MUTED}" text-anchor="end" font-family="{LABEL_FONT}" letter-spacing="0.05em">'
                      f'{_esc(x_unit)}</text>')
    _marks_layer(fr, doc, ctx.state.hidden_layers, ctx.panel, over=True)
    return _finish(fr.out, ctx.geo, _footer(fr.out, ctx.geo, ctx.notes, fr.bottom + 16))


def _draw_heatmap(ctx: _Ctx) -> str:
    doc = ctx.doc
    fld = ctx.data.get("field") or {}
    z = fld.get("z") or []
    vals = [v for row in z for v in row if _is_num(v)]
    if not vals:
        return _empty(ctx.geo, "heatmap", ctx.title, "no values in the field")
    rows, cols = len(z), len(z[0])
    xs = list(fld.get("x") or range(cols))
    ys_ = list(fld.get("y") or range(rows))
    zmin = fld.get("zmin") if _is_num(fld.get("zmin")) else min(vals)
    zmax = fld.get("zmax") if _is_num(fld.get("zmax")) else max(vals)
    cmap = palettes.sequential_name((doc.get("encoding") or {}).get("colormap"))
    unit = fld.get("unit") or ""
    geo = ctx.geo
    out = _root(geo, ctx.title, f"heatmap of {fld.get('name') or 'a field'}, {rows} by {cols}, "
                f"{_with_unit(min(vals), unit)} to {_with_unit(max(vals), unit)}", "heatmap")
    top = _heading(out, geo, ctx.title, ctx.subtitle) + 13.5 * 1.2
    axes = doc.get("axes") or {}
    tick = 11.5
    left = max(48.0, max(len(_num(v)) for v in (ys_[0], ys_[-1]) if _is_num(v)) * tick * 0.62 + 30) \
        if all(_is_num(v) for v in (ys_[0], ys_[-1])) else 56.0
    bar_w = 14
    bar_room = 78
    bottom_room = 13.5 * (3.4 if (axes.get("x") or {}).get("title") else 2.1)
    plot_w = geo.width - left - bar_room - 16
    plot_h = geo.height - top - bottom_room
    if (doc.get("encoding") or {}).get("aspect") == "equal":
        cell = min(plot_w / cols, plot_h / rows)
        plot_w, plot_h = cell * cols, cell * rows
    cw, ch = plot_w / cols, plot_h / rows
    total = rows * cols
    marks = _Marks(total)
    span = zmax - zmin or 1.0
    out.append('<g class="axk-marks" shape-rendering="crispEdges">')
    for r, row in enumerate(z):
        y = top + plot_h - (r + 1) * ch  # row 0 at the bottom, as y increases upward
        for c, v in enumerate(row):
            if not _is_num(v):
                continue
            colour = palettes.sequential(cmap, (v - zmin) / span)
            attrs = f'x="{left + c * cw:.2f}" y="{y:.2f}" width="{cw + 0.05:.2f}" height="{ch + 0.05:.2f}" fill="{colour}"'
            if marks.titled:
                label = f"{_cat(xs[c])}, {_cat(ys_[r])}: {_with_unit(v, unit, span)}"
                out.append(marks.el("rect", attrs, label))
            else:
                out.append(f"<rect {attrs}/>")
    out.append("</g>")
    # Values in the cells, for a map small enough to read them (a pin map):
    # light text on the dark end of the scale, dark text on the light end.
    if (doc.get("encoding") or {}).get("labels") and total <= 625 and min(cw, ch) >= 14:
        size = max(7.0, min(11.0, cw * 0.3))
        out.append(f'<g class="axk-cell-labels" font-size="{size:.1f}" text-anchor="middle" aria-hidden="true">')
        for r, row in enumerate(z):
            y = top + plot_h - (r + 0.5) * ch + size * 0.35
            for c, v in enumerate(row):
                if _is_num(v):
                    light = (v - zmin) / span < 0.55
                    out.append(f'<text x="{left + (c + 0.5) * cw:.1f}" y="{y:.1f}" '
                               f'fill="{"#ffffff" if light else "#1a1a1a"}">{_esc(_num(v, span))}</text>')
        out.append("</g>")
    nulls = total - len(vals)
    # Axes: numeric ticks at nice values, placed by index interpolation.
    for axis, values, n, horizontal in (("x", xs, cols, True), ("y", ys_, rows, False)):
        if all(_is_num(v) for v in values) and len(values) > 1:
            lo, hi = float(values[0]), float(values[-1])
            for t in _nice_ticks(min(lo, hi), max(lo, hi)):
                frac = (t - lo) / ((hi - lo) or 1)
                if not -0.001 <= frac <= 1.001:
                    continue
                if horizontal:
                    px = left + (frac * (n - 1) + 0.5) * cw
                    out.append(f'<text x="{px:.1f}" y="{top + plot_h + 13.5 * 1.45:.1f}" font-size="{tick}" '
                               f'fill="{MUTED}" text-anchor="middle">{_esc(_num(t, hi - lo))}</text>')
                else:
                    py = top + plot_h - (frac * (n - 1) + 0.5) * ch
                    out.append(f'<text x="{left - 8:.1f}" y="{py + 4:.1f}" font-size="{tick}" '
                               f'fill="{MUTED}" text-anchor="end">{_esc(_num(t, hi - lo))}</text>')
    xt = chart_marks.strip_html((axes.get("x") or {}).get("title") or "")
    yt = chart_marks.strip_html((axes.get("y") or {}).get("title") or "")
    if xt:
        out.append(f'<text x="{left + plot_w / 2:.1f}" y="{top + plot_h + 13.5 * 2.9:.1f}" font-size="11.5" '
                   f'fill="{MUTED}" text-anchor="middle" font-family="{LABEL_FONT}">{_esc(xt)}</text>')
    if yt:
        out.append(f'<text x="{left - 8:.1f}" y="{top - 8:.1f}" font-size="11" fill="{MUTED}" '
                   f'text-anchor="end" font-family="{LABEL_FONT}" letter-spacing="0.05em">{_esc(yt)}</text>')
    # The colour bar: the scale IS the legend, so it carries the unit.
    bx = left + plot_w + 22
    steps = 48
    for i in range(steps):
        t0 = i / steps
        y = top + plot_h - (i + 1) * plot_h / steps
        out.append(f'<rect x="{bx}" y="{y:.2f}" width="{bar_w}" height="{plot_h / steps + 0.3:.2f}" '
                   f'fill="{palettes.sequential(cmap, t0 + 0.5 / steps)}"/>')
    for t in _nice_ticks(zmin, zmax):
        frac = (t - zmin) / span
        if not -0.001 <= frac <= 1.001:
            continue
        py = top + plot_h - frac * plot_h
        out.append(f'<line x1="{bx + bar_w:.1f}" y1="{py:.1f}" x2="{bx + bar_w + 4:.1f}" y2="{py:.1f}" stroke="{INK}"/>')
        out.append(f'<text x="{bx + bar_w + 7:.1f}" y="{py + 4:.1f}" font-size="11" fill="{MUTED}">{_esc(_num(t, span))}</text>')
    out.append(f'<text x="{bx:.1f}" y="{top - 8:.1f}" font-size="11" fill="{MUTED}"{_quiet(unit)} '
               f'font-family="{LABEL_FONT}" letter-spacing="0.05em">{_esc(unit_label(unit, brief=True))}</text>')
    summary = (f"{fld.get('name') or ctx.title}: {rows} by {cols} values from {_with_unit(min(vals), unit, span)} "
               f"to {_with_unit(max(vals), unit, span)}, colour scale {cmap}")
    out.append(f'<rect x="{left:.1f}" y="{top:.1f}" width="{plot_w:.1f}" height="{plot_h:.1f}" fill="none" '
               f'stroke="{RULE}" data-axk-mark="" tabindex="0" aria-label="{_esc(summary)}"><title>{_esc(summary)}</title></rect>')
    notes = list(ctx.notes)
    if nulls:
        notes.append(f"{nulls} cell{'s' if nulls != 1 else ''} with no value, left unfilled")
    bottom = top + plot_h + bottom_room
    return _finish(out, geo, _footer(out, geo, notes, bottom + 4))


def _cat(v: Any) -> str:
    return _num(v) if _is_num(v) else str(v)


# ---------------------------------------------------------------------------
# Kinds without axes.
# ---------------------------------------------------------------------------


def _slices(ctx: _Ctx) -> list[tuple[str, float, str]]:
    series = ctx.series()
    if len(series) == 1 and len(series[0].get("y") or []) > 1:
        s = series[0]
        return [(str(x), float(v), s.get("unit") or "") for x, v in zip(_xs(s), s["y"], strict=False)
                if _is_num(v) and v > 0]
    return [(s["name"], float(s["y"][-1]), s.get("unit") or "") for s in series
            if s.get("y") and _is_num(s["y"][-1]) and s["y"][-1] > 0]


def _arc(cx: float, cy: float, r: float, a0: float, a1: float) -> str:
    """An SVG arc path from angle a0 to a1 (radians, clockwise from 12 o'clock)."""
    x0, y0 = cx + r * math.sin(a0), cy - r * math.cos(a0)
    x1, y1 = cx + r * math.sin(a1), cy - r * math.cos(a1)
    large = 1 if (a1 - a0) % (2 * math.pi) > math.pi else 0
    return f"M{x0:.2f},{y0:.2f} A{r:.2f},{r:.2f} 0 {large} 1 {x1:.2f},{y1:.2f}"


def _draw_pie(ctx: _Ctx) -> str:
    doc, geo = ctx.doc, ctx.geo
    kind = doc["kind"]
    slices = _slices(ctx)
    if not slices:
        return _empty(geo, kind, ctx.title, "nothing to divide")
    total = sum(v for _, v, _ in slices)
    unit = slices[0][2]
    hole = (doc.get("encoding") or {}).get("hole", 0.58 if kind == "donut" else 0.0)
    colours = _category_colours(len(slices), doc)
    out = _root(geo, ctx.title, f"{kind} of {len(slices)} parts totalling {_with_unit(total, unit)}", kind)
    top = _heading(out, geo, ctx.title, ctx.subtitle) + 10
    # The key needs room for a name, a value and a share; the pie gives way.
    key_w = 250.0
    r = max(30.0, min((geo.height - top - 18) / 2, geo.width * 0.24, (geo.width - geo.left - 46 - key_w) / 2))
    cx, cy = geo.left + r + 6, top + r
    marks = _Marks(len(slices))
    a = 0.0
    out.append('<g class="axk-marks">')
    for (name, v, _), colour in zip(slices, colours, strict=True):
        sweep = 2 * math.pi * v / total
        b = a + sweep
        pct = 100 * v / total
        label = f"{name}: {_with_unit(v, unit)} ({pct:.1f}%)"
        if len(slices) == 1:
            d = (f"M{cx:.2f},{cy - r:.2f} A{r:.2f},{r:.2f} 0 1 1 {cx - 0.01:.2f},{cy - r:.2f} Z")
        else:
            outer = _arc(cx, cy, r, a, b)
            if hole > 0:
                ri = r * hole
                x1, y1 = cx + ri * math.sin(b), cy - ri * math.cos(b)
                inner_back = _arc(cx, cy, ri, b, a).replace("A", "A", 1)
                x0i, y0i = cx + ri * math.sin(a), cy - ri * math.cos(a)
                large = 1 if sweep > math.pi else 0
                d = (outer + f" L{x1:.2f},{y1:.2f} A{ri:.2f},{ri:.2f} 0 {large} 0 {x0i:.2f},{y0i:.2f} Z")
                del inner_back
            else:
                d = f"M{cx:.2f},{cy:.2f} L" + outer[1:] + " Z"
        out.append(marks.el("path", f'd="{d}" fill="{colour}" stroke="{PAPER}" stroke-width="1.5"', label))
        if pct >= 7 and hole < 0.5:
            mid = (a + b) / 2
            tr = r * (0.62 if hole == 0 else (1 + hole) / 2)
            out.append(f'<text x="{cx + tr * math.sin(mid):.1f}" y="{cy - tr * math.cos(mid) + 4:.1f}" font-size="11" '
                       f'fill="#ffffff" text-anchor="middle" font-family="{LABEL_FONT}" font-weight="600">{pct:.0f}%</text>')
        a = b
    out.append("</g>")
    if hole > 0:
        out.append(f'<text x="{cx:.1f}" y="{cy + 2:.1f}" font-size="{max(14.0, r * 0.28):.1f}" fill="{INK}" '
                   f'text-anchor="middle" font-weight="600">{_esc(_num(total))}</text>')
        out.append(f'<text x="{cx:.1f}" y="{cy + 18:.1f}" font-size="11" fill="{MUTED}" text-anchor="middle" '
                   f'font-family="{LABEL_FONT}">total{(" " + _pretty(unit)) if declared(unit) else ""}</text>')
    # The key, beside the pie: name, value, share. A pie without numbers asks
    # the reader to compare angles, which is the thing people do worst.
    lx = cx + r + 34
    ly = top + max(8.0, r - len(slices) * 11)
    for i, ((name, v, _), colour) in enumerate(zip(slices, colours, strict=True)):
        y = ly + i * 22
        out.append(f'<rect x="{lx:.1f}" y="{y - 10:.1f}" width="12" height="12" rx="2" fill="{colour}"/>')
        value_text = _with_unit(v, unit)
        value_x = geo.width - 64
        room = max(24.0, value_x - len(value_text) * 12 * 0.56 - 10 - (lx + 18))
        out.append(f'<text x="{lx + 18:.1f}" y="{y:.1f}" font-size="12" fill="{INK}" font-family="{LABEL_FONT}">'
                   f'{_esc(_fit(name, room, 12))}</text>')
        out.append(f'<text x="{value_x:.1f}" y="{y:.1f}" font-size="12" fill="{MUTED}" text-anchor="end">'
                   f'{_esc(value_text)}</text>')
        out.append(f'<text x="{geo.width - 16:.1f}" y="{y:.1f}" font-size="12" fill="{INK}" text-anchor="end" '
                   f'font-weight="600">{100 * v / total:.1f}%</text>')
    bottom = max(cy + r, ly + len(slices) * 22) + 10
    return _finish(out, geo, _footer(out, geo, ctx.notes, bottom + 4))


def _value(ctx: _Ctx) -> dict[str, Any]:
    v = dict(ctx.data.get("value") or {})
    return v


def _rows(ctx: _Ctx) -> list[dict[str, Any]]:
    """Value rows: one ``data.value``, or one per series (its last reading)."""
    if ctx.data.get("value") is not None:
        return [_value(ctx)]
    rows = []
    for s in ctx.series():
        ys = [v for v in s.get("y") or [] if _is_num(v)]
        rows.append({"label": s["name"], "value": ys[-1] if ys else None, "unit": s.get("unit") or "",
                     "min": s.get("low", 0.0), "max": s.get("high"), "trend": s.get("y") or [],
                     "target": None})
    return rows


def _bounds(row: Mapping[str, Any]) -> tuple[float, float]:
    lo = row.get("min") if _is_num(row.get("min")) else 0.0
    hi = row.get("max")
    if not _is_num(hi):
        hi = 100.0 if row.get("unit") in ("%", "percent") else max(1.0, (row.get("value") or 0) * 1.25)
    return float(lo), float(hi)


def _value_colour(row: Mapping[str, Any], doc: Mapping[str, Any]) -> str:
    unit = row.get("unit") or ""
    colours, _ = assign_colours([(str(row.get("label") or doc.get("title")), unit)])
    target = row.get("target")
    if _is_num(target) and _is_num(row.get("value")) and row["value"] > target:
        return chart_marks.tone("alarm")
    return palettes.series_colour(colours[0])


def _draw_progress(ctx: _Ctx) -> str:
    doc, geo = ctx.doc, ctx.geo
    rows = _rows(ctx)
    out = _root(geo, ctx.title, f"progress of {len(rows)} value{'s' if len(rows) != 1 else ''}", "progress")
    top = _heading(out, geo, ctx.title, ctx.subtitle) + 10
    marks = _Marks(len(rows))
    bar_h = 12
    pitch = 46
    label_w = max(60.0, min(geo.width * 0.32, max(len(str(r.get("label") or "")) for r in rows) * 7.0 + 12))
    x0 = geo.left + label_w
    w = geo.width - x0 - 96
    for i, row in enumerate(rows):
        y = top + i * pitch + 14
        lo, hi = _bounds(row)
        v = row.get("value")
        unit = row.get("unit") or ""
        frac = 0.0 if not _is_num(v) else max(0.0, min(1.0, (v - lo) / ((hi - lo) or 1)))
        colour = _value_colour(row, doc)
        name = str(row.get("label") or doc.get("title"))
        out.append(f'<text x="{geo.left}" y="{y + 10:.1f}" font-size="12.5" fill="{INK}" font-family="{LABEL_FONT}">'
                   f'{_esc(_fit(name, label_w - 10, 12.5))}</text>')
        out.append(f'<rect x="{x0:.1f}" y="{y:.1f}" width="{w:.1f}" height="{bar_h}" rx="{bar_h / 2}" fill="{TRACK}"/>')
        label = f"{name}: {_with_unit(v, unit)} of {_with_unit(hi, unit)} ({frac * 100:.0f}%)"
        out.append(marks.el("rect", f'x="{x0:.1f}" y="{y:.1f}" width="{max(bar_h, w * frac):.1f}" height="{bar_h}" '
                            f'rx="{bar_h / 2}" fill="{colour}"', label))
        if _is_num(row.get("target")):
            tx = x0 + w * max(0.0, min(1.0, (row["target"] - lo) / ((hi - lo) or 1)))
            out.append(f'<line x1="{tx:.1f}" y1="{y - 4:.1f}" x2="{tx:.1f}" y2="{y + bar_h + 4:.1f}" '
                       f'stroke="{INK}" stroke-width="2"/>')
        shown = f"{frac * 100:.0f}%" if unit in ("%", "percent") or not declared(unit) else _with_unit(v, unit)
        out.append(f'<text x="{geo.width - 16:.1f}" y="{y + 10:.1f}" font-size="13" fill="{INK}" text-anchor="end" '
                   f'font-weight="600">{_esc(shown)}</text>')
    bottom = top + len(rows) * pitch
    return _finish(out, geo, _footer(out, geo, ctx.notes, bottom + 4))


def _draw_gauge(ctx: _Ctx) -> str:
    doc, geo = ctx.doc, ctx.geo
    row = _rows(ctx)[0]
    v, unit = row.get("value"), row.get("unit") or ""
    lo, hi = _bounds(row)
    out = _root(geo, ctx.title, f"gauge: {_with_unit(v, unit)} between {_with_unit(lo, unit)} and "
                f"{_with_unit(hi, unit)}", "gauge")
    top = _heading(out, geo, ctx.title, ctx.subtitle) + 6
    r = max(36.0, min((geo.height - top - 30) / 1.25, geo.width / 3.4))
    cx, cy = geo.width / 2, top + r + 6
    a0, a1 = -2 * math.pi / 3, 2 * math.pi / 3
    width = max(10.0, r * 0.2)
    out.append(f'<path d="{_arc(cx, cy, r, a0, a1)}" fill="none" stroke="{TRACK}" stroke-width="{width:.1f}" stroke-linecap="round"/>')
    ranges = row.get("ranges") or []
    for i, edge in enumerate(ranges):
        if _is_num(edge) and lo < edge < hi:
            aa = a0 + (a1 - a0) * (edge - lo) / (hi - lo)
            out.append(f'<line x1="{cx + (r - width) * math.sin(aa):.1f}" y1="{cy - (r - width) * math.cos(aa):.1f}" '
                       f'x2="{cx + (r + width) * math.sin(aa):.1f}" y2="{cy - (r + width) * math.cos(aa):.1f}" '
                       f'stroke="{MUTED}" stroke-width="1"/>')
        del i
    frac = 0.0 if not _is_num(v) else max(0.0, min(1.0, (v - lo) / ((hi - lo) or 1)))
    colour = _value_colour(row, doc)
    label = f"{row.get('label') or ctx.title}: {_with_unit(v, unit)} ({frac * 100:.0f}% of range)"
    if frac > 0.002:
        out.append(_Marks(1).el("path", f'd="{_arc(cx, cy, r, a0, a0 + (a1 - a0) * frac)}" fill="none" '
                                f'stroke="{colour}" stroke-width="{width:.1f}" stroke-linecap="round"', label))
    target = row.get("target")
    if _is_num(target):
        at = a0 + (a1 - a0) * max(0.0, min(1.0, (target - lo) / ((hi - lo) or 1)))
        out.append(f'<line x1="{cx + (r - width * 0.9) * math.sin(at):.1f}" y1="{cy - (r - width * 0.9) * math.cos(at):.1f}" '
                   f'x2="{cx + (r + width * 0.9) * math.sin(at):.1f}" y2="{cy - (r + width * 0.9) * math.cos(at):.1f}" '
                   f'stroke="{INK}" stroke-width="2.5"/>')
        out.append(f'<text x="{cx + (r + width * 1.5) * math.sin(at):.1f}" y="{cy - (r + width * 1.5) * math.cos(at) + 4:.1f}" '
                   f'font-size="10.5" fill="{INK}" text-anchor="{"start" if math.sin(at) > 0.1 else ("end" if math.sin(at) < -0.1 else "middle")}" '
                   f'font-family="{LABEL_FONT}">limit {_esc(_with_unit(target, unit))}</text>')
    out.append(f'<text x="{cx:.1f}" y="{cy + r * 0.12:.1f}" font-size="{max(18.0, r * 0.36):.1f}" fill="{INK}" '
               f'text-anchor="middle" font-weight="600" data-axk-mark="" tabindex="0" aria-label="{_esc(label)}">'
               f'{_esc(_num(v) if _is_num(v) else "—")}<title>{_esc(label)}</title></text>')
    out.append(f'<text x="{cx:.1f}" y="{cy + r * 0.12 + 18:.1f}" font-size="12" fill="{MUTED}" text-anchor="middle" '
               f'font-family="{LABEL_FONT}">{_esc(_pretty(unit) if declared(unit) else "no unit")}</text>')
    for edge, ang, anchor in ((lo, a0, "middle"), (hi, a1, "middle")):
        out.append(f'<text x="{cx + r * math.sin(ang):.1f}" y="{cy - r * math.cos(ang) + width + 12:.1f}" font-size="11" '
                   f'fill="{MUTED}" text-anchor="{anchor}">{_esc(_num(edge))}</text>')
    bottom = cy + r * 0.62 + 16
    return _finish(out, geo, _footer(out, geo, ctx.notes, bottom + 4))


def _spark_path(values: list[Any], x: float, y: float, w: float, h: float) -> tuple[str, tuple[float, float] | None]:
    pts = [(i, v) for i, v in enumerate(values) if _is_num(v)]
    if len(pts) < 2:
        return "", None
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    n = max(len(values) - 1, 1)
    d, last = [], None
    prev = None
    for i, v in pts:
        px = x + w * i / n
        py = y + h - h * ((v - lo) / ((hi - lo) or 1))
        d.append(("M" if prev is None or i != prev + 1 else "L") + f"{px:.1f},{py:.1f}")
        prev, last = i, (px, py)
    return "".join(d), last


def _draw_kpi(ctx: _Ctx) -> str:
    doc, geo = ctx.doc, ctx.geo
    rows = _rows(ctx)
    out = _root(geo, ctx.title, f"{len(rows)} current value{'s' if len(rows) != 1 else ''}", "kpi")
    top = _heading(out, geo, ctx.title if len(rows) > 1 else "", ctx.subtitle if len(rows) > 1 else "")
    n = len(rows)
    cols = min(n, max(1, int(geo.width // 170)))
    tile_w = (geo.width - 2 * 12) / cols
    # A tile with no trend to draw does not keep the room a sparkline needs.
    has_trend = any(sum(1 for t in (r.get("trend") or []) if _is_num(t)) >= 2 for r in rows)
    tile_h = 112 if has_trend else 80
    marks = _Marks(n)
    for i, row in enumerate(rows):
        c, r = i % cols, i // cols
        x = 12 + c * tile_w
        y = top + 6 + r * (tile_h + 8)
        unit = row.get("unit") or ""
        v = row.get("value")
        name = str(row.get("label") or ctx.title)
        colour = _value_colour(row, doc)
        trend = [t for t in row.get("trend") or []]
        delta = row.get("delta")
        live = [t for t in trend if _is_num(t)]
        if delta is None and len(live) >= 2:
            delta = live[-1] - live[0]
        label = f"{name}: {_with_unit(v, unit)}" + (f", change {('+' if delta >= 0 else '')}{_with_unit(delta, unit)}"
                                                     if _is_num(delta) else "")
        if c:
            out.append(f'<line x1="{x:.1f}" y1="{y + 6:.1f}" x2="{x:.1f}" y2="{y + tile_h - 10:.1f}" stroke="{RULE}"/>')
        out.append(f'<text x="{x + 12:.1f}" y="{y + 18:.1f}" font-size="12" fill="{MUTED}" '
                   f'font-family="{LABEL_FONT}">{_esc(_fit(name, tile_w - 24, 12))}</text>')
        fmt = row.get("format")
        shown = (fmt.format(v) if isinstance(fmt, str) and _is_num(v) else (_num(v) if _is_num(v) else "—"))
        size = 30 if tile_w > 150 else 24
        out.append(f'<text x="{x + 12:.1f}" y="{y + 54:.1f}" font-size="{size}" fill="{INK}" font-weight="600"'
                   f'{marks.attrs(label)}>{_esc(shown)}<title>{_esc(label)}</title></text>')
        if declared(unit):
            out.append(f'<text x="{x + 14 + len(shown) * size * 0.56:.1f}" y="{y + 54:.1f}" font-size="14" '
                       f'fill="{MUTED}" font-family="{LABEL_FONT}">{_esc(_pretty(unit))}</text>')
        if _is_num(delta):
            arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "■")
            out.append(f'<text x="{x + 12:.1f}" y="{y + 74:.1f}" font-size="11.5" fill="{MUTED}" '
                       f'font-family="{LABEL_FONT}">{arrow} {_esc(("+" if delta > 0 else "") + _with_unit(delta, unit))}'
                       f'{" over the window" if len(live) >= 2 and row.get("delta") is None else ""}</text>')
        d, last = _spark_path(trend, x + 12, y + 82, tile_w - 30, 20)
        if d:
            out.append(f'<path d="{d}" fill="none" stroke="{colour}" stroke-width="1.5" stroke-linejoin="round"/>')
            out.append(f'<circle cx="{last[0]:.1f}" cy="{last[1]:.1f}" r="2.4" fill="{colour}"/>')
    rows_n = (n + cols - 1) // cols
    bottom = top + 6 + rows_n * (tile_h + 8)
    return _finish(out, geo, _footer(out, geo, ctx.notes, bottom + 4))


def _draw_sparkline(ctx: _Ctx) -> str:
    doc, geo = ctx.doc, ctx.geo
    series = ctx.series()
    if not series:
        return _empty(geo, "sparkline", ctx.title, "no series")
    colours = _series_colours(series, doc, ctx.preferred)
    out = _root(geo, ctx.title, f"sparkline of {', '.join(s['name'] for s in series)}", "sparkline")
    top = 6.0
    pitch = (geo.height - 12) / len(series)
    marks = _Marks(len(series))
    for i, (s, colour) in enumerate(zip(series, colours, strict=True)):
        y = top + i * pitch
        ys = [v for v in s.get("y") or [] if _is_num(v)]
        unit = s.get("unit") or ""
        name_w = min(geo.width * 0.3, len(s["name"]) * 7 + 10)
        out.append(f'<text x="8" y="{y + pitch / 2 + 4:.1f}" font-size="12" fill="{INK}" font-family="{LABEL_FONT}">'
                   f'{_esc(s["name"])}</text>')
        d, last = _spark_path(s.get("y") or [], 8 + name_w, y + 4, geo.width - name_w - 100, pitch - 10)
        label = (f"{s['name']}: last {_with_unit(ys[-1], unit)}, low {_with_unit(min(ys), unit)}, high "
                 f"{_with_unit(max(ys), unit)}") if ys else f"{s['name']}: no readings"
        if d:
            out.append(marks.el("path", f'd="{d}" fill="none" stroke="{colour}" stroke-width="1.5"', label))
            out.append(f'<circle cx="{last[0]:.1f}" cy="{last[1]:.1f}" r="2.4" fill="{colour}"/>')
        if ys:
            out.append(f'<text x="{geo.width - 8:.1f}" y="{y + pitch / 2 + 4:.1f}" font-size="12" fill="{INK}" '
                       f'text-anchor="end" font-weight="600">{_esc(_with_unit(ys[-1], unit))}</text>')
    return _finish(out, geo, geo.height - 6)


def _draw_bullet(ctx: _Ctx) -> str:
    doc, geo = ctx.doc, ctx.geo
    rows = _rows(ctx)
    out = _root(geo, ctx.title, f"bullet chart of {len(rows)} value{'s' if len(rows) != 1 else ''}", "bullet")
    top = _heading(out, geo, ctx.title, ctx.subtitle) + 8
    marks = _Marks(len(rows))
    label_w = max(60.0, min(geo.width * 0.3, max(len(str(r.get("label") or ctx.title)) for r in rows) * 7.0 + 14))
    x0 = geo.left + label_w
    w = geo.width - x0 - 30
    for i, row in enumerate(rows):
        y = top + i * 54
        lo, hi = _bounds(row)
        unit = row.get("unit") or ""
        v, target = row.get("value"), row.get("target")
        edges = [lo] + [e for e in (row.get("ranges") or []) if _is_num(e) and lo < e < hi] + [hi]
        for k in range(len(edges) - 1):
            a = x0 + w * (edges[k] - lo) / (hi - lo)
            b = x0 + w * (edges[k + 1] - lo) / (hi - lo)
            shade = 0.34 - 0.1 * k
            out.append(f'<rect x="{a:.1f}" y="{y:.1f}" width="{b - a:.1f}" height="22" fill="{MUTED}" '
                       f'fill-opacity="{max(0.08, shade):.2f}"/>')
        name = str(row.get("label") or ctx.title)
        out.append(f'<text x="{geo.left}" y="{y + 15:.1f}" font-size="12.5" fill="{INK}" font-family="{LABEL_FONT}">'
                   f'{_esc(_fit(name, label_w - 10, 12.5))}</text>')
        if _is_num(v):
            frac = max(0.0, min(1.0, (v - lo) / ((hi - lo) or 1)))
            label = f"{name}: {_with_unit(v, unit)}" + (f" against target {_with_unit(target, unit)}" if _is_num(target) else "")
            out.append(marks.el("rect", f'x="{x0:.1f}" y="{y + 7:.1f}" width="{w * frac:.1f}" height="8" '
                                f'fill="{_value_colour(row, doc)}"', label))
        if _is_num(target):
            tx = x0 + w * max(0.0, min(1.0, (target - lo) / ((hi - lo) or 1)))
            out.append(f'<line x1="{tx:.1f}" y1="{y + 3:.1f}" x2="{tx:.1f}" y2="{y + 19:.1f}" stroke="{INK}" stroke-width="2.5"/>')
        for t in _nice_ticks(lo, hi):
            tx = x0 + w * (t - lo) / ((hi - lo) or 1)
            out.append(f'<text x="{tx:.1f}" y="{y + 36:.1f}" font-size="10.5" fill="{MUTED}" text-anchor="middle">{_esc(_num(t, hi - lo))}</text>')
    bottom = top + len(rows) * 54
    if declared(rows[0].get("unit") or ""):
        out.append(f'<text x="{geo.width - 30:.1f}" y="{bottom - 4:.1f}" font-size="11" fill="{MUTED}" text-anchor="end" '
                   f'font-family="{LABEL_FONT}">{_esc(unit_label(rows[0]["unit"], brief=True))}</text>')
    return _finish(out, geo, _footer(out, geo, ctx.notes, bottom + 10))


def _draw_state(ctx: _Ctx) -> str:
    ctx.notes.append("a state is drawn held between readings")
    doc = dict(ctx.doc)
    doc["kind"] = "step"
    ctx.doc = doc
    return _draw_lines(ctx)



# ---------------------------------------------------------------------------
# Flame and icicle.
# ---------------------------------------------------------------------------

#: Row height and label size of a flame graph, at screen scale.
FLAME_ROW = 18.0


def _flame_colour(frame_name: str, category: str | None, by: str, categories: list[str],
                  doc: Mapping[str, Any]) -> str:
    """Category colour, or a colour fixed by the frame's NAME: never random, so
    the same function is the same colour in every render and every profile."""
    if by == "category" and category is not None and category in categories:
        pal = palettes.categorical(doc.get("palette"))
        return pal[categories.index(category) % len(pal)]
    pal = palettes.FLAME
    return pal[zlib.crc32(frame_name.encode("utf-8")) % len(pal)]


def _draw_flame(ctx: _Ctx) -> str:
    from . import flame_data as fd

    doc, geo = ctx.doc, ctx.geo
    tree = ctx.data.get("tree") or {}
    unit = str(tree.get("unit") or "")
    enc = {**(doc.get("encoding") or {}), **ctx.state.encoding}
    direction = enc.get("direction", "flame")
    root = fd.read(tree)
    if enc.get("invert"):
        root = fd.invert(root)
    top, ancestors = fd.subtree(root, ctx.state.zoom)
    total = root.value or 1.0
    controls = doc.get("controls") or []
    zoom_id = next((c["id"] for c in controls if c["bind"]["to"] == "zoom"), None)

    out = _root(geo, ctx.title, f"{'icicle' if direction == 'icicle' else 'flame'} graph of {root.count()} frames, "
                f"{_with_unit(root.value, unit)} in all", "flame")
    y = _heading(out, geo, ctx.title, ctx.subtitle)
    left, width = 12.0, geo.width - 24.0
    # The breadcrumb: where the zoom is, and each step back up is a click.
    chain = [*ancestors, top] if top is not root else [root]
    names = [n.name for n in chain]
    crumbs = [(";".join(names[1:k + 1]), node.name) for k, node in enumerate(chain)]
    x = left
    y += 4
    out.append('<g class="axk-breadcrumb" font-size="12">')
    for i, (path, name) in enumerate(crumbs):
        last = i == len(crumbs) - 1
        label = name if i else (f"{name} (reset zoom)" if len(crumbs) > 1 else name)
        attrs = (f' data-axk-control="{_esc(zoom_id)}" data-axk-value="{_esc(path)}" tabindex="0" role="button" '
                 f'aria-label="{_esc("Zoom to " + name if i else "Reset zoom")}"' if zoom_id and not last else "")
        weight = ' font-weight="600"' if last else ""
        out.append(f'<text x="{x:.1f}" y="{y + 10:.1f}" fill="{INK if last else MUTED}" font-family="{LABEL_FONT}"'
                   f'{weight}{attrs}>{_esc(label)}</text>')
        x += len(label) * 12 * 0.56 + 8
        if not last:
            out.append(f'<text x="{x:.1f}" y="{y + 10:.1f}" fill="{MUTED}" font-family="{LABEL_FONT}">›</text>')
            x += 14
    out.append("</g>")
    y += 18
    hits, match = fd.search(root, ctx.state.search)
    status = []
    if ctx.state.search:
        status.append(f"{match.count} {'frame matches' if match.count == 1 else 'frames match'} \u201c{ctx.state.search}\u201d: "
                      f"{100 * match.value / total:.1f}% of the total")
    if enc.get("invert"):
        status.append("inverted: each frame's callers are above it")
    if status:
        out.append(f'<text x="{left:.1f}" y="{y + 10:.1f}" font-size="11.5" fill="{INK}" font-family="{LABEL_FONT}" '
                   f'role="status">{_esc("  ·  ".join(status))}</text>')
        y += 18
    frames, folded = fd.layout(top, width_px=width, min_px=1.0, ancestors=ancestors)
    depth = max((f.depth for f in frames), default=0) + 1
    plot_top = y + 4
    plot_h = depth * FLAME_ROW

    def row_y(d: int) -> float:
        return plot_top + d * FLAME_ROW if direction == "icicle" else plot_top + plot_h - (d + 1) * FLAME_ROW

    categories = sorted({f.node.category for f in frames if f.node is not None and f.node.category})
    by = enc.get("colour_by", "category" if categories else "name")
    order = sorted(range(len(frames)), key=lambda i: -frames[i].value)
    focusable = set(order[:FOCUSABLE_MAX])
    titled = len(frames) <= TITLED_MAX
    searching = bool(ctx.state.search)
    out.append(f'<g class="axk-marks" font-size="11" font-family="{LABEL_FONT}">')
    n_anc = len(ancestors)
    for i, f in enumerate(frames):
        fx = left + f.x * width
        fw = max(f.w * width, 1.0)
        fy = row_y(f.depth)
        is_anc = f.depth < n_anc
        if f.node is None:
            fill = MUTED
        elif is_anc:
            fill = TRACK
        else:
            fill = _flame_colour(f.name, f.node.category, by, categories, doc)
        hit = f.node is not None and id(f.node) in hits
        opacity = "1" if not searching or hit or is_anc else "0.32"
        pct_total = 100 * f.value / total
        pct_parent = 100 * f.value / (f.parent_value or f.value or 1.0)
        label = (f"{f.name}: {_with_unit(f.value, unit)}, {pct_total:.1f}% of total, {pct_parent:.1f}% of its caller"
                 + (f" ({f.merged} frames too narrow to draw)" if f.merged else ""))
        attrs = 'data-axk-mark=""'
        if i in focusable:
            attrs += f' tabindex="0" aria-label="{_esc(label)}"'
        if zoom_id and f.node is not None and f.node is not top:
            attrs += f' data-axk-control="{_esc(zoom_id)}" data-axk-value="{_esc(";".join(f.path))}"'
        out.append(f'<g {attrs} opacity="{opacity}">')
        stroke = f' stroke="{INK}" stroke-width="1.5"' if hit else f' stroke="{PAPER}" stroke-width="0.5"'
        out.append(f'<rect x="{fx:.2f}" y="{fy:.1f}" width="{fw:.2f}" height="{FLAME_ROW - 1:.1f}" rx="1.5" '
                   f'fill="{fill}"{stroke}/>')
        room = int((fw - 6) / (11 * 0.56))
        if room >= 3:
            text = f.name if len(f.name) <= room else f.name[: room - 1] + "…"
            ink = INK if is_anc else "#1a1a1a"
            out.append(f'<text x="{fx + 3:.2f}" y="{fy + FLAME_ROW - 5.5:.1f}" fill="{ink}">{_esc(text)}</text>')
        if titled:
            out.append(f"<title>{_esc(label)}</title>")
        out.append("</g>")
    out.append("</g>")
    notes = ctx.notes
    if folded:
        notes.append(f"{folded} frame{'s' if folded != 1 else ''} narrower than a pixel folded into "
                     f"\u201c{fd.OTHER}\u201d slivers; zoom in to see them")
    bottom = plot_top + plot_h + 10
    return _finish(out, geo, _footer(out, geo, notes, bottom + 6))


# ---------------------------------------------------------------------------
# 3D: chrome and a static projection here; the live scene is vtk.js in the
# browser, drawn into the box this marks, from the payload this resolves.
# ---------------------------------------------------------------------------

SCENE_KINDS = frozenset({"mesh3d", "surface", "scatter3d", "volume"})


def _shade(colour: str, factor: float) -> str:
    c = colour.lstrip("#")
    r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
    return "#%02x%02x%02x" % (int(r * factor), int(g * factor), int(b * factor))


def _draw_scene(ctx: _Ctx) -> str:
    from . import scene_data as sd

    doc, geo = ctx.doc, ctx.geo
    raw = ctx.data.get("scene") or {}
    enc = {**(doc.get("encoding") or {}), **ctx.state.encoding}
    if doc["kind"] == "volume" and "grid" in raw:
        scene = sd.volume_slice(raw["grid"], axis=enc.get("slice_axis", "z"),
                                at=enc.get("slice_at"), length_unit=str(raw.get("length_unit") or ""))
        if enc.get("iso") is not None:
            ctx.notes.append("isosurfaces are drawn in the 3D view; this static view shows the slice")
    else:
        scene = sd.read(raw)
    # A surface's height is often tiny beside its extent (a shape of 0..1 over
    # tens of centimetres): drawn at true scale it is flat, and its quads read
    # as stacked bands. "auto" (Plotly's default too) scales height to half
    # the larger extent and says by how much; "data" keeps true scale.
    if doc["kind"] == "surface" and enc.get("aspect", "auto") == "auto" and scene.points:
        xs_ = [p[0] for p in scene.points]
        ys_ = [p[1] for p in scene.points]
        zs_ = [p[2] for p in scene.points]
        zspan = max(zs_) - min(zs_)
        extent = max(max(xs_) - min(xs_), max(ys_) - min(ys_))
        if zspan > 0 and extent > 0:
            factor = 0.5 * extent / zspan
            if not 0.67 <= factor <= 1.5:
                z0 = min(zs_)
                scene.points = [(p[0], p[1], z0 + (p[2] - z0) * factor) for p in scene.points]
                ctx.notes.append(f"height drawn at {_num(factor)}x its scale to show the shape")
    names = list(scene.scalars)
    scalar = enc.get("scalar") if enc.get("scalar") in scene.scalars else (names[0] if names else None)
    unit, assoc, values = scene.scalars.get(scalar, ("", "cell", []))
    if doc["kind"] == "volume":
        unit = unit or str((raw.get("scalars") or {}).get(next(iter(raw.get("scalars") or {}), ""), {}).get("unit") or "")
    cmap = palettes.sequential_name(enc.get("colormap"))
    stops_light = palettes.scene_stops(cmap, "light")
    stops_dark = palettes.scene_stops(cmap, "dark")

    def scene_colour(t: float, factor: float = 1.0) -> str:
        """light-dark() of the same value on each theme's part of the scale."""
        a = _shade(palettes.sample(stops_light, t), factor)
        b = _shade(palettes.sample(stops_dark, t), min(1.0, factor + 0.15))
        return a if a == b else f"light-dark({a},{b})"

    finite = [v for v in values if v is not None]
    lo, hi = (min(finite), max(finite)) if finite else (0.0, 1.0)
    span = (hi - lo) or 1.0
    keep = sd.cut(scene, axis=enc.get("slice_axis", "z") if enc.get("cutaway") else None,
                  at=enc.get("slice_at") if enc.get("cutaway") else None, planes=enc.get("clip") or ())
    offsets = sd.explode(scene, float(enc.get("explode") or 0))
    az, el = sd.camera_of(enc.get("camera", "iso"))

    def cell_value(ci: int) -> float | None:
        if not values:
            return None
        if assoc == "cell":
            return values[ci]
        vs = [values[i] for i in scene.cells[ci] if values[i] is not None]
        return sum(vs) / len(vs) if vs else None

    out = _root(geo, ctx.title, f"3D {doc['kind']} of {len(scene.cells)} "
                f"{'points' if scene.markers else 'cells'}" + (f", coloured by {scalar}" if scalar else ""), doc["kind"])
    top = _heading(out, geo, ctx.title, ctx.subtitle) + 8
    bar_room = 86 if scalar else 0
    box = (16.0, top, geo.width - 32.0 - bar_room, max(160.0, geo.height - top - 30))
    bx, by, bw, bh = box

    # Project, fit to the box, paint far to near.
    polys = []
    for ci in keep:
        off = offsets.get(ci, (0.0, 0.0, 0.0))
        pts3 = [tuple(scene.points[i][k] + off[k] for k in range(3)) for i in scene.cells[ci]]
        proj = [sd.project(p, az, el) for p in pts3]
        polys.append((sum(p[2] for p in proj) / len(proj), ci, proj, pts3))
    xs = [p[0] for _, _, pr, _ in polys for p in pr] or [0.0, 1.0]
    ys = [p[1] for _, _, pr, _ in polys for p in pr] or [0.0, 1.0]
    zoom = float((enc.get("camera") or {}).get("zoom", 1.0)) if isinstance(enc.get("camera"), Mapping) else 1.0
    scale = min(bw / ((max(xs) - min(xs)) or 1.0), bh / ((max(ys) - min(ys)) or 1.0)) * 0.92 * zoom
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2

    def to_px(p: tuple[float, float, float]) -> tuple[float, float]:
        return bx + bw / 2 + (p[0] - cx) * scale, by + bh / 2 - (p[1] - cy) * scale

    polys.sort(key=lambda t: t[0])
    opacity = float(enc.get("opacity", 1.0))
    edges = bool(enc.get("edges"))
    els = scene.elements
    focus_by_el = els is not None and len(set(els)) <= FOCUSABLE_MAX
    groups: dict[str, list[str]] = {}
    el_values: dict[str, list[float]] = {}
    out.append(f'<rect class="axk-scene" x="{bx:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="none" '
               f'data-axk-scene="{bx:.1f} {by:.1f} {bw:.1f} {bh:.1f}"/>')
    out.append(f'<g class="axk-scene-fallback" opacity="{opacity:g}">')
    loose: list[str] = []
    for _, ci, proj, pts3 in polys:
        v = cell_value(ci)
        light = sd.normal_light(pts3, az, el) if not scene.markers else 1.0
        fill = scene_colour((v - lo) / span, light) if v is not None else "#9aa3ad"
        if scene.markers:
            x, y = to_px(proj[0])
            svg = f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" style="fill:{fill}"/>'
        else:
            d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in (to_px(p) for p in proj)) + "Z"
            stroke = f' stroke="{RULE}" stroke-width="0.4"' if edges else f' style="stroke:{fill}" stroke-width="0.3"'
            svg = f'<path d="{d}" style="fill:{fill}"{stroke}/>'
        if focus_by_el and els is not None:
            groups.setdefault(els[ci], []).append(svg)
            if v is not None:
                el_values.setdefault(els[ci], []).append(v)
        else:
            loose.append(svg)
    out.extend(loose)
    for el_id, parts in groups.items():
        vs = el_values.get(el_id) or []
        mean = sum(vs) / len(vs) if vs else None
        label = f"{el_id}: {_with_unit(mean, unit, span) if mean is not None else 'no value'}" + (
            f" ({scalar})" if scalar else "")
        out.append(f'<g data-axk-mark="" tabindex="0" aria-label="{_esc(label)}"><title>{_esc(label)}</title>'
                   + "".join(parts) + "</g>")
    out.append("</g>")
    if not focus_by_el:
        summary = (f"{ctx.title}: {len(keep)} of {len(scene.cells)} {'points' if scene.markers else 'cells'} shown"
                   + (f", {scalar} from {_with_unit(lo, unit, span)} to {_with_unit(hi, unit, span)}" if scalar else ""))
        out.append(f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="none" data-axk-mark="" '
                   f'tabindex="0" aria-label="{_esc(summary)}"><title>{_esc(summary)}</title></rect>')
    if scalar:
        cbx = bx + bw + 30
        steps = 48
        for i in range(steps):
            y = by + bh - (i + 1) * bh / steps
            out.append(f'<rect x="{cbx}" y="{y:.2f}" width="14" height="{bh / steps + 0.3:.2f}" '
                       f'style="fill:{scene_colour((i + 0.5) / steps)}"/>')
        for t in _nice_ticks(lo, hi):
            frac = (t - lo) / span
            if -0.001 <= frac <= 1.001:
                py = by + bh - frac * bh
                out.append(f'<line x1="{cbx + 14:.1f}" y1="{py:.1f}" x2="{cbx + 18:.1f}" y2="{py:.1f}" stroke="{INK}"/>')
                out.append(f'<text x="{cbx + 21:.1f}" y="{py + 4:.1f}" font-size="11" fill="{MUTED}">'
                           f'{_esc(_num(t, span))}</text>')
        out.append(f'<text x="{cbx:.1f}" y="{by - 8:.1f}" font-size="11" fill="{MUTED}"{_quiet(unit)} '
                   f'font-family="{LABEL_FONT}" letter-spacing="0.05em">{_esc(unit_label(unit, brief=True))}</text>')
    if len(keep) < len(scene.cells):
        ctx.notes.append(f"{len(scene.cells) - len(keep)} of {len(scene.cells)} cells cut away")
    # What the browser's 3D view draws, already cut, exploded and coloured by
    # the same rules, so the live view and this one cannot disagree.
    kept_pts: dict[Any, int] = {}
    pts_out, polys_out, cell_vals, el_out, point_vals = [], [], [], [], []
    for ci in keep:
        off = offsets.get(ci, (0.0, 0.0, 0.0))
        poly = []
        for i in scene.cells[ci]:
            key = (i if not off[0] and not off[1] else (i, ci))
            if key not in kept_pts:
                kept_pts[key] = len(pts_out)
                p = scene.points[i]
                pts_out.append([round(p[0] + off[0], 4), round(p[1] + off[1], 4), round(p[2] + off[2], 4)])
                if assoc == "point" and values:
                    pv = values[i]
                    point_vals.append(None if pv is None else round(pv, 6))
            poly.append(kept_pts[key])
        polys_out.append(poly)
        cv = cell_value(ci)
        cell_vals.append(None if cv is None else round(cv, 6))
        if els is not None:
            el_out.append(els[ci])
    ctx.scene = {
        "box": [round(bx, 1), round(by, 1), round(bw, 1), round(bh, 1)],
        "points": pts_out, "polys": polys_out, "markers": scene.markers,
        "cell_values": cell_vals, "range": [lo, hi], "scalar": scalar, "unit": unit,
        "scalars": names, "stops": stops_light, "stops_dark": stops_dark, "colormap": cmap,
        **({"point_values": point_vals} if point_vals else {}),
        "camera": {"azimuth": az, "elevation": el, "zoom": zoom}, "opacity": opacity, "edges": edges,
        **({"elements": el_out} if els is not None else {}),
        "length_unit": scene.length_unit,
    }
    return _finish(out, geo, _footer(out, geo, ctx.notes, by + bh + 16))

_DRAW: dict[str, Callable[[_Ctx], str]] = {
    "area": _draw_lines, "step": _draw_lines, "bar": _draw_bar, "histogram": _draw_histogram,
    "box": _draw_box, "scatter": _draw_scatter, "heatmap": _draw_heatmap, "pie": _draw_pie,
    "donut": _draw_pie, "progress": _draw_progress, "gauge": _draw_gauge, "kpi": _draw_kpi,
    "sparkline": _draw_sparkline, "bullet": _draw_bullet, "state": _draw_state, "flame": _draw_flame,
    "mesh3d": _draw_scene, "surface": _draw_scene, "scatter3d": _draw_scene, "volume": _draw_scene,
}

#: Default canvas per kind, when the caller names no size.
_DEFAULT_SIZE = {
    "kpi": (360, 96), "gauge": (360, 260), "progress": (520, 0), "sparkline": (360, 64),
    "bullet": (560, 0), "pie": (560, 300), "donut": (560, 300), "flame": (920, 120),
    "mesh3d": (920, 560), "surface": (920, 520), "scatter3d": (920, 520), "volume": (920, 520),
}


#: Kinds drawn at their content's height whatever height the caller offers.
_COMPACT = frozenset({"kpi", "sparkline", "progress", "bullet", "gauge", "flame"})


def _geometry(kind: str, rows: int, width: int | None, height: int | None) -> Geometry:
    dw, dh = _DEFAULT_SIZE.get(kind, (920, 420))
    if kind in ("progress", "bullet") and not dh:
        dh = 60 + rows * (46 if kind == "progress" else 54)
    w = max(240, min(int(width or dw), Geometry.MAX_WIDTH))
    h = max(48, int(height or dh))
    if kind in _COMPACT:
        # A tile is as tall as what it says. Stretched to a panel's height it
        # is a number floating in empty space.
        h = min(h, dh) if dh else dh
    return Geometry(width=w, height=h)


def _draw(ctx: _Ctx) -> str:
    kind = ctx.doc["kind"]
    if kind == "line":
        series = ctx.series()
        if _x_kind(ctx.doc, series) == "time":
            return _draw_time_line(ctx)
        return _draw_lines(ctx)
    return _DRAW[kind](ctx)


def _draw_panels(ctx: _Ctx) -> str:
    """Subplots and small multiples: each panel a full chart, laid in a grid."""
    doc, geo = ctx.doc, ctx.geo
    layout = doc.get("layout") or {}
    panels = layout.get("panels") or []
    cols = int(layout.get("columns") or (2 if len(panels) > 1 else 1))
    rows = (len(panels) + cols - 1) // cols
    out = _root(geo, ctx.title, f"{len(panels)} panels: {', '.join(p.get('title') or p['id'] for p in panels)}", doc["kind"])
    top = _heading(out, geo, ctx.title, ctx.subtitle) + 4
    pw = (geo.width - 8) / cols
    ph = max(200.0, (geo.height - top - 6) / rows)
    row_top = [top]
    row_h = [0.0] * rows
    drawn: list[tuple[int, int, str]] = []
    for i, p in enumerate(panels):
        c, r = i % cols, i // cols
        sub = {k: v for k, v in doc.items() if k not in ("layout", "title", "subtitle", "controls")}
        sub["title"] = p.get("title") or p["id"]
        if p.get("axes"):
            sub["axes"] = {**(doc.get("axes") or {}), **p["axes"]}
        child = _Ctx(doc=sub, data=ctx.data, state=ctx.state, notes=[], preferred=ctx.preferred,
                     panel=p["id"], geo=Geometry(width=int(pw), height=int(ph)))
        svg = _draw(child)
        ctx.notes.extend(f"{sub['title']}: {n}" for n in child.notes)
        import re as _re
        got = _re.search(r'height="(\d+(?:\.\d+)?)"', svg)
        row_h[r] = max(row_h[r], float(got.group(1)) if got else ph)
        drawn.append((c, r, svg))
    for r in range(1, rows):
        row_top.append(row_top[-1] + row_h[r - 1] + 6)
    for c, r, svg in drawn:
        out.append(svg.replace("<svg ", f'<svg x="{4 + c * pw:.1f}" y="{row_top[r]:.1f}" ', 1))
    bottom = row_top[-1] + row_h[-1]
    return _finish(out, geo, _footer(out, geo, ctx.notes, bottom + 4))


def _apply_window(data: dict[str, Any], window: tuple[Any, Any] | None) -> dict[str, Any]:
    if not window or "series" not in data:
        return data
    a, b = window
    ta, tb = _instant(a), _instant(b)

    def inside(x: Any) -> bool:
        if ta and tb:
            m = _instant(x)
            return m is not None and ta <= m <= tb
        return _is_num(x) and _is_num(a) and _is_num(b) and a <= x <= b

    out = []
    for s in data["series"]:
        xs = _xs(s)
        keep = [i for i, x in enumerate(xs) if inside(x)]
        s2 = dict(s)
        s2["x"] = [xs[i] for i in keep]
        s2["y"] = [s["y"][i] for i in keep]
        out.append(s2)
    return {**data, "series": out}


def render_spec(
    document: Mapping[str, Any],
    *,
    state: Mapping[str, Any] | None = None,
    width: int | None = None,
    height: int | None = None,
    resolve: Resolver | None = None,
    preferred_colours: Mapping[str, str] | None = None,
) -> Rendered:
    """Draw a ChartSpec 2.0 under *state*. Raises :class:`~.unified_spec.UnifiedSpecError`."""
    doc = us.parse(document)
    resolved = us.resolve_state(doc, state)
    data = us.select_data(doc, resolved)
    notes: list[str] = []
    bounds = None
    if "query" in data or "verb" in data:
        if resolve is None:
            geo = _geometry(doc["kind"], 1, width, height)
            svg = _empty(geo, doc["kind"], doc["title"],
                         "this chart reads its data from a store; no figure source was given to read it")
            return Rendered(svg=svg, notes=["no figure source"], state=resolved.values,
                            controls=_controls_now(doc, resolved), spec_id=us.spec_id(doc))
        data = dict(resolve(doc, data, resolved))
        bounds = data.pop("bounds", None)
    data = _apply_window(data, resolved.window if doc["kind"] != "line" else None)
    rows = len(data.get("series") or []) or 1
    geo = _geometry(doc["kind"], rows, width, height)
    # Every note is drawn inside the figure, including what the controls could
    # not take, so a page never has to repeat them under it.
    notes.extend(resolved.notes)
    ctx = _Ctx(doc=doc, data=data, state=resolved, geo=geo, notes=notes, preferred=preferred_colours)
    if doc.get("layout", {}).get("panels"):
        svg = _draw_panels(ctx)
    else:
        svg = _draw(ctx)
    return Rendered(svg=svg, notes=ctx.notes, state=resolved.values,
                    controls=_controls_now(doc, resolved), spec_id=us.spec_id(doc), readout=ctx.readout,
                    bounds=bounds, scene=ctx.scene)


def _controls_now(doc: Mapping[str, Any], resolved: us.ResolvedState) -> list[dict[str, Any]]:
    out = []
    for c in doc.get("controls") or []:
        c2 = dict(c)
        c2["value"] = resolved.values.get(c["id"])
        if c["bind"]["to"] == "frame" and not c2.get("options"):
            items = ((doc.get("data") or {}).get("frames") or {}).get("items") or []
            c2["options"] = [{"value": it.get("value"), "label": it.get("label") or str(it.get("value"))}
                             for it in items]
        out.append(c2)
    return out


def normalise_svg(svg: str) -> str:
    """The figure with nothing that may legitimately differ between two draws.

    Today that is nothing: the renderer is deterministic and never reads
    ``source``. Kept as the seam the seamlessness test compares through, so a
    future per-draw attribute is stripped here rather than breaking the test.
    """
    return svg.strip()


__all__ = ["FOCUSABLE_MAX", "Rendered", "Resolver", "normalise_svg", "render_spec"]
