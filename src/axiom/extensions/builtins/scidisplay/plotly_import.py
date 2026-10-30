# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A Plotly figure, or a Trame app's figures and widgets, read as ChartSpec 2.0.

Plotly figures arrive as JSON: ``{"data": [trace, ...], "layout": {...},
"frames": [...]}``. That is what ``plotly.py``'s ``Figure.to_plotly_json()``
produces and what a Trame app's ``trame-plotly`` widget keeps in its server
state, so one reader covers a notebook, a script and a running Trame app.

The reader does not draw. It decides, per figure, one of two things and says
why (ADR-214):

- **Inside the supported subset** it becomes a ChartSpec 2.0 document, with
  ``source`` naming the original figure by reference and pinning its bytes by
  digest. The platform renderer draws that document exactly as it draws one a
  person authored, because it is the same kind of object: the seamlessness
  test holds an imported figure and its native equivalent to byte-identical
  SVG.
- **Outside it**, :attr:`Imported.refused` names every reason and the figure
  goes to the fenced fallback (:func:`theme_figure` over
  :func:`sanitize_figure`), clearly labelled. Never half of each: a picture
  that silently loses an annotation says something its author did not.

What the author set that the platform answers for itself (a colour, a
template, a font, a rainbow colour scale) is not a refusal; it is listed in
``replaced`` so nobody wonders where their magenta went.

## The supported subset (importer 2.0)

======================  ====================================================
scatter, scattergl      lines over time -> ``line`` (the Charts page renderer);
                        markers only -> ``scatter``; ``line.shape`` hv/vh ->
                        ``step``; ``fill``/``stackgroup`` -> ``area`` (stacked
                        when grouped); numeric or log x (a spectrum) -> ``line``
bar                     vertical or ``orientation: h``; ``barmode``
                        group/stack/relative; ``barnorm: percent``
pie                     ``pie``; ``hole > 0`` -> ``donut``
heatmap                 ``heatmap``; any colour scale -> the sequential palette
histogram               ``histogram`` (``nbinsx`` -> bins)
box                     ``box``
indicator               number -> ``kpi`` (several -> a strip); ``gauge`` ->
                        ``gauge``; ``gauge.shape: bullet`` -> ``bullet``
layout                  title (``<br>`` splits a subtitle), axis titles and
                        log types, ``y2`` overlaid, subplots (``xaxisN``,
                        ``yaxisN``, ``make_subplots`` titles) -> ``layout.panels``,
                        annotations and shapes -> ``marks``
update menus            ``restyle`` of ``visible`` -> a segmented/dropdown
                        control over series groups; ``relayout`` of
                        annotations/shapes -> a toggle over a mark layer;
                        ``animate`` play/pause -> playback
sliders + frames        ``data.frames`` and a slider (and playback)
Trame widgets           ``VSlider``/``VRangeSlider``/``VSelect``/``VSwitch``/
                        ``VCheckbox``/``VBtnToggle`` -> controls
======================  ====================================================

Refused: 3D, geographic, polar, ternary, contour, violin, waterfall, funnel,
financial and hierarchical traces; two trace families in one panel; update
menus whose method is ``update``; a third y axis.
"""

from __future__ import annotations

import base64
import copy
import math
import re
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import palettes
from . import unified_spec as us
from .chart_marks import strip_html
from .chart_svg import LABEL_FONT

IMPORTER_VERSION = "2.0.0"

#: Trace type -> the kind family it maps to.
_FAMILY = {
    "scatter": "xy", "scattergl": "xy", "bar": "bar", "pie": "pie", "heatmap": "heatmap",
    "heatmapgl": "heatmap", "histogram": "histogram", "box": "box", "indicator": "indicator",
    "icicle": "icicle",
    "mesh3d": "mesh3d", "surface": "surface", "scatter3d": "scatter3d", "isosurface": "volume",
    "volume": "volume",
}

#: Hierarchy traces the platform does not draw as themselves, and what to do instead.
_HIERARCHY_REFUSED = {
    "treemap": "a treemap trace: the platform draws a hierarchy as a flame or icicle graph, so "
               "re-export it as an icicle (same ids, labels, parents and values) to import it natively",
    "cone": "a cone trace (a vector field): the platform's 3D kinds draw meshes, surfaces, points and scalar "
            "volumes; a vector field is not one of them yet",
    "streamtube": "a streamtube trace (a vector field): not one of the platform's 3D kinds yet",
    "sunburst": "a sunburst trace: the platform draws a hierarchy as a flame or icicle graph, so "
                "re-export it as an icicle (same ids, labels, parents and values) to import it natively",
}

#: Presentation the platform replaces rather than honours. Reported, not refused.
_REPLACED_LAYOUT = (
    "template", "font", "colorway", "paper_bgcolor", "plot_bgcolor", "legend", "margin",
    "hovermode", "width", "height", "showlegend", "bargap", "bargroupgap", "autosize",
)

_TITLE_UNIT = re.compile(r"[\(\[]\s*([^()\[\]]{1,16}?)\s*[\)\]]\s*$")
_DTYPES = {"f8": "d", "f4": "f", "i1": "b", "u1": "B", "i2": "h", "u2": "H", "i4": "i", "u4": "I"}
_AXIS = re.compile(r"^([xy])axis(\d*)$")
_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass
class Imported:
    """One figure, read: a spec, or refused with every reason."""

    spec: dict[str, Any] | None = None
    refused: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    #: The figure for the fenced fallback, sanitised and themed. Set when refused.
    fallback: dict[str, Any] | None = None

    @property
    def native(self) -> bool:
        return self.spec is not None and not self.refused


# ---------------------------------------------------------------------------
# Reading values.
# ---------------------------------------------------------------------------


def _decode(raw: Any) -> list[Any] | None:
    """A Plotly array as a list: typed (``bdata``) or plain; ``None`` if neither."""
    if isinstance(raw, Mapping) and "bdata" in raw:
        code = _DTYPES.get(str(raw.get("dtype", "")))
        if code is None:
            return None
        blob = base64.b64decode(raw["bdata"])
        size = struct.calcsize(code)
        values = list(struct.unpack(f"<{len(blob) // size}{code}", blob[: (len(blob) // size) * size]))
        shape = raw.get("shape")
        if isinstance(shape, str) and "," in shape:
            rows, cols = (int(p) for p in shape.split(","))
            return [values[r * cols:(r + 1) * cols] for r in range(rows)]
        return values
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return [(_decode(v) if isinstance(v, Mapping) else v) for v in raw]
    return None


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _clean(v: Any) -> Any:
    """A number as the JSON a person would write: whole floats as ints."""
    n = _num(v)
    if n is None:
        return None
    return int(n) if n.is_integer() and abs(n) < 2**53 else n


def _x(v: Any) -> Any:
    if isinstance(v, str):
        t = v.strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d", t):
            t = t.replace(" ", "T", 1)
        return t
    n = _num(v)
    return _clean(n) if n is not None else (None if v is None else str(v))


def _text(raw: Any) -> str:
    if isinstance(raw, Mapping):
        raw = raw.get("text", "")
    return strip_html(raw)


def _title_parts(raw: Any) -> tuple[str, str]:
    text = raw.get("text", "") if isinstance(raw, Mapping) else str(raw or "")
    parts = re.split(r"<br\s*/?>", text, maxsplit=1, flags=re.I)
    title = strip_html(parts[0])
    sub = strip_html(parts[1]) if len(parts) > 1 else ""
    return title, sub


def _unit_from_title(axis: Mapping[str, Any]) -> str:
    m = _TITLE_UNIT.search(_text(axis.get("title")))
    return m.group(1).strip() if m else ""


def _meta(trace: Mapping[str, Any]) -> Mapping[str, Any]:
    meta = trace.get("meta")
    if isinstance(meta, Mapping) and isinstance(meta.get("axiom"), Mapping):
        return meta["axiom"]
    return {}


def _slug(text: str, fallback: str) -> str:
    s = _SLUG.sub("-", text.lower()).strip("-")[:48]
    return s or fallback


def _tone_of(colour: Any) -> str:
    c = str(colour or "").lower()
    if any(k in c for k in ("red", "#f00", "#ff0000", "crimson", "#c0392b", "#d62728")):
        return "alarm"
    if any(k in c for k in ("orange", "yellow", "gold", "#ffa", "#ff0", "amber")):
        return "warn"
    if "green" in c:
        return "ok"
    if "blue" in c:
        return "info"
    return "neutral"


# ---------------------------------------------------------------------------
# Panels: subplots from axis anchors and domains.
# ---------------------------------------------------------------------------


def _axis_ref(trace: Mapping[str, Any], letter: str) -> str:
    ref = str(trace.get(f"{letter}axis") or letter)
    return ref if ref != f"{letter}1" else letter


def _layout_axis(layout: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
    letter, num = ref[0], ref[1:]
    return layout.get(f"{letter}axis{num}") or {}


def _panels(data: list[Mapping[str, Any]], layout: Mapping[str, Any]) -> tuple[list[tuple[str, str]], bool]:
    """The (xref, yref) panels in trace order, and whether y2 is an overlay."""
    pairs: list[tuple[str, str]] = []
    overlay = False
    for t in data:
        x, y = _axis_ref(t, "x"), _axis_ref(t, "y")
        yax = _layout_axis(layout, y)
        if y != "y" and yax.get("overlaying"):
            overlay = True
            y = str(yax["overlaying"])
        if (x, y) not in pairs:
            pairs.append((x, y))
    return pairs, overlay


# ---------------------------------------------------------------------------
# The import.
# ---------------------------------------------------------------------------


def _xy_kind(trace: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    mode = str(trace.get("mode") or "lines")
    line = trace.get("line") or {}
    enc: dict[str, Any] = {}
    if trace.get("stackgroup"):
        return "area", {"stack": "percent" if trace.get("groupnorm") == "percent" else "stacked"}
    if trace.get("fill") in ("tozeroy", "tonexty", "tozerox"):
        if str(line.get("shape") or "") in ("hv", "vh"):
            enc["curve"] = "step"
        return "area", enc
    if "lines" not in mode and "markers" in mode:
        return "scatter", enc
    if str(line.get("shape") or "") in ("hv", "vh", "hvh", "vhv"):
        return "step", enc
    return "line", enc


def _is_time(xs: list[Any], axis: Mapping[str, Any]) -> bool:
    if axis.get("type") == "date":
        return True
    sample = [x for x in xs if x is not None][:20]
    return bool(sample) and all(isinstance(x, str) and re.match(r"^\d{4}-\d{2}-\d{2}", x) for x in sample)


def import_figure(
    figure: Mapping[str, Any],
    *,
    ref: str = "inline",
    chart_id: str | None = None,
    fmt: str = "plotly",
    revision: int = 1,
    site: str | None = None,
) -> Imported:
    """Read *figure* as a ChartSpec 2.0 document, or refuse it with reasons.

    ``ref`` names where the caller keeps the original (a library object, a
    file, a Trame state key); the spec carries it with the figure's digest.
    ``site``, when given and every trace names its ``meta.axiom.channel``, makes
    the spec a reference to the store (``data.query``) instead of a copy.
    """
    out = Imported()
    data = [t for t in (figure.get("data") or []) if isinstance(t, Mapping)]
    layout = figure.get("layout") or {}
    if not isinstance(layout, Mapping) or not data:
        out.refused.append("not a Plotly figure with traces: expected {data: [...], layout: {...}}")
        return _refuse(out, figure)

    for key in _REPLACED_LAYOUT:
        if key in layout:
            out.replaced.append(f"layout.{key}")

    types = [str(t.get("type") or "scatter") for t in data]
    for i, kind in enumerate(types):
        if kind in _HIERARCHY_REFUSED:
            out.refused.append(_HIERARCHY_REFUSED[kind])
            continue
        if kind not in _FAMILY:
            out.refused.append(f"{_text(data[i].get('name')) or f'trace {i}'!r} is a {kind} trace, "
                               "which the platform renderer does not draw")
    families = {_FAMILY.get(k) for k in types}
    if len(families) > 1:
        out.refused.append(f"the figure mixes {', '.join(sorted(f for f in families if f))} traces in one chart")
    for key in layout:
        m = _AXIS.match(key)
        if m and m.group(1) == "y" and m.group(2) and int(m.group(2)) >= 3:
            yax = layout[key]
            if isinstance(yax, Mapping) and yax.get("overlaying"):
                out.refused.append(f"a third y axis ({key}); two units is the most one figure labels")
    if out.refused:
        return _refuse(out, figure)

    family = families.pop()
    title, subtitle = _title_parts(layout.get("title"))
    doc: dict[str, Any] = {"schema_version": us.SCHEMA_VERSION}
    if chart_id:
        doc["id"] = chart_id

    if family == "indicator":
        _indicators(doc, data, out)
    elif family == "pie":
        _pie(doc, data[0], out)
        if len(data) > 1:
            out.refused.append("more than one pie in a figure")
    elif family in ("mesh3d", "surface", "scatter3d", "volume"):
        _scene3d(doc, family, data, layout, out)
    elif family == "icicle":
        _icicle(doc, data[0], out)
        if len(data) > 1:
            out.refused.append("more than one hierarchy in a figure")
    elif family == "heatmap":
        _heatmap(doc, data[0], layout, out)
        if len(data) > 1:
            out.refused.append("more than one field in a figure")
    else:
        _cartesian(doc, family, data, layout, out)
    if out.refused:
        return _refuse(out, figure)

    doc["title"] = title or doc.pop("_title", "") or "Imported chart"
    doc.pop("_title", None)
    if subtitle:
        doc["subtitle"] = subtitle
    _menus(doc, figure, data, layout, out)
    if out.refused:
        return _refuse(out, figure)
    if site:
        _as_query(doc, data, site)
    doc["source"] = {
        "type": "imported", "format": fmt, "figure": ref, "digest": us.figure_digest(figure),
        "importer_version": IMPORTER_VERSION, "revision": revision,
    }
    if out.replaced:
        doc["source"]["replaced"] = sorted(set(out.replaced))
    ordered = {k: doc[k] for k in us.DOCUMENT_KEYS if k in doc}
    problems = us.validate(ordered)
    if problems:
        out.refused.extend(f"the translation is not a valid spec: {p}" for p in problems)
        return _refuse(out, figure)
    out.spec = us.parse(ordered)
    return out


def _refuse(out: Imported, figure: Mapping[str, Any]) -> Imported:
    out.spec = None
    out.fallback = theme_figure(sanitize_figure(figure))
    return out


def _series_of(trace: Mapping[str, Any], *, axis_unit: str, kind: str, index: int) -> dict[str, Any]:
    meta = _meta(trace)
    name = _text(trace.get("name")) or f"trace {index + 1}"
    s: dict[str, Any] = {"name": name}
    unit = str(meta.get("unit") or "") or axis_unit
    if unit:
        s["unit"] = unit
    return s


def _cartesian(doc: dict[str, Any], family: str, data: list[Mapping[str, Any]],
               layout: Mapping[str, Any], out: Imported) -> None:
    kinds = []
    enc: dict[str, Any] = {}
    for t in data:
        if family == "xy":
            k, e = _xy_kind(t)
            kinds.append(k)
            enc.update(e)
        else:
            kinds.append(family)
    if len(set(kinds)) > 1:
        # A scatter-lines figure with one markers trace is still a line figure.
        if set(kinds) <= {"line", "scatter"}:
            kinds = ["line"] * len(kinds)
            enc["show_points"] = True
        else:
            out.refused.append(f"the figure mixes {', '.join(sorted(set(kinds)))} marks in one chart")
            return
    kind = kinds[0]
    doc["kind"] = kind
    pairs, _overlay = _panels(data, layout)
    multi = len(pairs) > 1
    series: list[dict[str, Any]] = []
    axes: dict[str, Any] = {}
    horizontal = family == "bar" and any(t.get("orientation") == "h" for t in data)
    xaxis = layout.get("xaxis") or {}
    for i, t in enumerate(data):
        xref, yref = _axis_ref(t, "x"), _axis_ref(t, "y")
        yax = _layout_axis(layout, yref)
        on_y2 = yref != "y" and bool(yax.get("overlaying"))
        if family in ("histogram", "box"):
            values = _decode(t.get("x") if family == "histogram" and t.get("x") is not None else t.get("y"))
            if values is None:
                out.refused.append(f"{_text(t.get('name')) or f'trace {i}'!r} has no values")
                return
            unit = _unit_from_title(xaxis if family == "histogram" else yax)
            s = _series_of(t, axis_unit=unit, kind=kind, index=i)
            s["y"] = [_clean(v) for v in values]
        else:
            xs = _decode(t.get("x"))
            ys = _decode(t.get("y"))
            if horizontal:
                xs, ys = ys, xs
            if ys is None:
                out.refused.append(f"{_text(t.get('name')) or f'trace {i}'!r} has no y values")
                return
            if xs is None:
                xs = list(range(len(ys)))
            vax = _layout_axis(layout, xref if horizontal else yref)
            unit = _unit_from_title(vax)
            s = _series_of(t, axis_unit=unit, kind=kind, index=i)
            s["x"] = [_x(v) for v in xs]
            s["y"] = [_clean(v) for v in ys]
            line = t.get("line") or {}
            dashed = str(line.get("dash") or "solid") not in ("solid", "")
            meta = _meta(t)
            if bool(meta.get("modelled", dashed)):
                s["modelled"] = True
            if meta.get("against"):
                s["against"] = str(meta["against"])
            if on_y2:
                s["axis"] = "y2"
        if multi:
            s["panel"] = f"p{pairs.index((xref, str(yax.get('overlaying') or yref) if on_y2 else yref)) + 1}"
        colourful = (t.get("marker") or {}).get("color") or (t.get("line") or {}).get("color")
        if colourful:
            out.replaced.append(f"{s['name']!r} colour")
        if t.get("hovertemplate"):
            out.replaced.append(f"{s['name']!r} hover template")
        series.append(s)
    doc["data"] = {"series": series}

    # Axes.
    x_title = _text(xaxis.get("title"))
    if family == "bar" and horizontal:
        x_title = ""
    x: dict[str, Any] = {}
    if xaxis.get("type") == "log":
        x["type"] = "log"
    if x_title and family not in ("box",):
        x["title"] = x_title
    if x:
        axes["x"] = x
    yax = layout.get("yaxis") or {}
    if yax.get("type") == "log" and not multi:
        axes["y"] = {"type": "log"}
    y2 = layout.get("yaxis2") or {}
    if y2.get("overlaying") and y2.get("type") == "log":
        axes["y2"] = {"type": "log"}
    if axes and kind in us.registered_kinds():
        doc["axes"] = axes

    if family == "bar":
        mode = layout.get("barmode")
        if layout.get("barnorm") == "percent" or any(t.get("barnorm") == "percent" for t in data):
            enc["stack"] = "percent"
        elif mode in ("stack", "relative"):
            enc["stack"] = "stacked"
        if horizontal:
            enc["orientation"] = "horizontal"
    if family == "histogram":
        nb = data[0].get("nbinsx")
        if isinstance(nb, int) and nb > 0:
            enc["bins"] = nb
    if family == "box" and any(t.get("boxpoints") == "all" for t in data):
        enc["show_points"] = True
    allowed = us.lookup_kind(kind).encoding
    dropped = sorted(set(enc) - allowed)
    for k in dropped:
        enc.pop(k)
    if enc:
        doc["encoding"] = enc

    if multi:
        _make_panels(doc, pairs, layout, out)
    marks = _marks(layout.get("annotations") or [], layout.get("shapes") or [], pairs, layout,
                   exclude=doc.pop("_title_annotations", set()))
    if marks:
        doc["marks"] = marks


def _make_panels(doc: dict[str, Any], pairs: list[tuple[str, str]], layout: Mapping[str, Any],
                 out: Imported) -> None:
    panels = []
    xs_domains = []
    for i, (xref, yref) in enumerate(pairs):
        xd = tuple(_layout_axis(layout, xref).get("domain") or (0, 1))
        yd = tuple(_layout_axis(layout, yref).get("domain") or (0, 1))
        xs_domains.append(xd)
        panel: dict[str, Any] = {"id": f"p{i + 1}", "_dom": (xd, yd)}
        yax = _layout_axis(layout, yref)
        if yax.get("type") == "log":
            panel["axes"] = {"y": {"type": "log"}}
        panels.append(panel)
    # make_subplots titles are paper annotations sitting on a domain's top edge.
    used: set[int] = set()
    for i, a in enumerate(layout.get("annotations") or []):
        if not isinstance(a, Mapping) or a.get("xref") != "paper" or a.get("yref") != "paper":
            continue
        ax, ay = _num(a.get("x")), _num(a.get("y"))
        for p in panels:
            (x0, x1), (y0, y1) = p["_dom"]
            if ax is not None and ay is not None and x0 - 0.01 <= ax <= x1 + 0.01 and abs(ay - y1) < 0.05 \
                    and "title" not in p:
                p["title"] = _text(a.get("text"))
                used.add(i)
                break
    # Grid order: top row first, then left to right.
    panels.sort(key=lambda p: (-p["_dom"][1][1], p["_dom"][0][0]))
    columns = len({round(d[0], 3) for d in xs_domains}) or 1
    for p in panels:
        p.pop("_dom")
    doc["layout"] = {"panels": panels, "columns": min(columns, 6)}
    doc["_title_annotations"] = used


def _marks(annotations: list[Any], shapes: list[Any], pairs: list[tuple[str, str]],
           layout: Mapping[str, Any], *, exclude: set[int] = frozenset(), layer: str | None = None) -> list[dict[str, Any]]:
    """Annotations and shapes as marks. A labelled event is ONE rule, not two."""
    multi = len(pairs) > 1

    def panel_of(xref: str, yref: str) -> str | None:
        if not multi:
            return None
        x, y = xref.replace(" domain", ""), yref.replace(" domain", "")
        for i, (px, py) in enumerate(pairs):
            if (px == x or x == "paper") and (py == y or y == "paper") and not (x == y == "paper"):
                return f"p{i + 1}"
        return None

    marks: list[dict[str, Any]] = []
    rules_at: dict[tuple[Any, str | None], dict[str, Any]] = {}
    for s in shapes:
        if not isinstance(s, Mapping):
            continue
        xref, yref = str(s.get("xref") or "x"), str(s.get("yref") or "y")
        kind = s.get("type") or "line"
        colour = (s.get("line") or {}).get("color") or s.get("fillcolor")
        m: dict[str, Any] = {}
        if kind == "line" and s.get("x0") == s.get("x1") and ("domain" in yref or yref == "paper"):
            m = {"type": "rule", "axis": "x", "value": _x(s.get("x0"))}
        elif kind == "line" and s.get("y0") == s.get("y1") and ("domain" in xref or xref == "paper"):
            m = {"type": "rule", "axis": "y", "value": _clean(s.get("y0"))}
        elif kind == "rect" and ("domain" in xref or xref == "paper"):
            m = {"type": "band", "axis": "y", "from": _clean(s.get("y0")), "to": _clean(s.get("y1"))}
        elif kind == "rect" and ("domain" in yref or yref == "paper"):
            m = {"type": "band", "axis": "x", "from": _x(s.get("x0")), "to": _x(s.get("x1"))}
        else:
            continue
        tone = _tone_of(colour)
        if tone != "neutral":
            m["tone"] = tone
        if layer:
            m["layer"] = layer
        p = panel_of(xref, yref)
        if p:
            m["panel"] = p
        if m["type"] == "rule" and m["axis"] == "x":
            rules_at[(m["value"], p)] = m
        marks.append(m)
    for i, a in enumerate(annotations):
        if i in exclude or not isinstance(a, Mapping):
            continue
        text = _text(a.get("text"))
        if not text:
            continue
        xref, yref = str(a.get("xref") or "x"), str(a.get("yref") or "y")
        p = panel_of(xref, yref)
        hit = rules_at.get((_x(a.get("x")), p))
        if hit is not None and "label" not in hit:
            hit["label"] = text
            continue
        if xref == "paper" and yref == "paper":
            continue  # a free caption on the page, not a mark on the data
        m = {"type": "annotation", "text": text}
        if a.get("x") is not None and "domain" not in xref:
            m["x"] = _x(a.get("x"))
        if a.get("y") is not None and "domain" not in yref and yref != "paper":
            m["y"] = _clean(a.get("y"))
        if layer:
            m["layer"] = layer
        if p:
            m["panel"] = p
        marks.append(m)
    return marks


def _pie(doc: dict[str, Any], t: Mapping[str, Any], out: Imported) -> None:
    labels = _decode(t.get("labels")) or []
    values = _decode(t.get("values")) or []
    hole = _num(t.get("hole")) or 0.0
    doc["kind"] = "donut" if hole > 0 else "pie"
    s: dict[str, Any] = {"name": _text(t.get("name")) or "values"}
    unit = str(_meta(t).get("unit") or "")
    if unit:
        s["unit"] = unit
    s["x"] = [str(strip_html(v)) for v in labels]
    s["y"] = [_clean(v) for v in values]
    doc["data"] = {"series": [s]}
    if hole > 0 and abs(hole - 0.58) > 1e-9:
        doc["encoding"] = {"hole": round(hole, 3)}
    if (t.get("marker") or {}).get("colors"):
        out.replaced.append("slice colours")


def _camera(layout: Mapping[str, Any]) -> dict[str, float] | None:
    """Plotly's scene.camera.eye as the azimuth and elevation the renderer takes."""
    eye = ((layout.get("scene") or {}).get("camera") or {}).get("eye")
    if not isinstance(eye, Mapping):
        return None
    ex, ey, ez = (_num(eye.get(k)) or 0.0 for k in ("x", "y", "z"))
    return {"azimuth": round(math.degrees(math.atan2(ex, -ey)), 2),
            "elevation": round(math.degrees(math.atan2(ez, math.hypot(ex, ey))), 2)}


def _scene3d(doc: dict[str, Any], family: str, data: list[Mapping[str, Any]],
             layout: Mapping[str, Any], out: Imported) -> None:
    if len(data) > 1:
        out.refused.append("more than one 3D trace in a figure; import each as its own chart")
        return
    t = data[0]
    doc["kind"] = family
    unit = str(_meta(t).get("unit") or "") or _text((t.get("colorbar") or {}).get("title"))
    m = _TITLE_UNIT.search(unit)
    if m:
        unit = m.group(1).strip()
    scene: dict[str, Any] = {}
    xs, ys, zs = (_decode(t.get(k)) for k in ("x", "y", "z"))
    if family == "mesh3d":
        if not (xs and ys and zs) or not all(t.get(k) is not None for k in ("i", "j", "k")):
            out.refused.append("a mesh3d without i, j, k is a point cloud Plotly triangulates itself; "
                               "give the triangles explicitly to import it")
            return
        ii, jj, kk = (_decode(t.get(k)) for k in ("i", "j", "k"))
        scene["mesh"] = {"points": [[_clean(a), _clean(b), _clean(c)] for a, b, c in zip(xs, ys, zs, strict=False)],
                         "cells": [[int(a), int(b), int(c)] for a, b, c in zip(ii, jj, kk, strict=False)]}
        intensity = _decode(t.get("intensity"))
        if intensity is not None:
            assoc = "cell" if t.get("intensitymode") == "cell" else "point"
            scene["scalars"] = {_text(t.get("name")) or "value": {
                **({"unit": unit} if unit else {}), "association": assoc, "values": [_clean(v) for v in intensity]}}
        if t.get("facecolor") is not None or t.get("vertexcolor") is not None or t.get("color"):
            out.replaced.append("mesh colours (drawn by its scalar on the sequential scale)")
    elif family == "surface":
        z = _decode(t.get("z"))
        if not z or not isinstance(z[0], list):
            out.refused.append("a surface without a 2-D z")
            return
        grid: dict[str, Any] = {"z": [[_clean(v) for v in row] for row in z]}
        if xs is not None and not isinstance(xs[0], list):
            grid["x"] = [_clean(v) for v in xs]
        if ys is not None and not isinstance(ys[0], list):
            grid["y"] = [_clean(v) for v in ys]
        if t.get("name"):
            grid["name"] = _text(t.get("name"))
        if unit:
            grid["unit"] = unit
        scene["grid"] = grid
    elif family == "scatter3d":
        scene["points"] = [[_clean(a), _clean(b), _clean(c)] for a, b, c in zip(xs or [], ys or [], zs or [], strict=False)]
        colour = _decode((t.get("marker") or {}).get("color"))
        if colour is not None and all(_num(v) is not None for v in colour):
            scene["scalars"] = {_text(t.get("name")) or "value": {
                **({"unit": unit} if unit else {}), "association": "point", "values": [_clean(v) for v in colour]}}
    else:  # isosurface / volume: values on a grid of x, y, z points
        values = _decode(t.get("value"))
        if not (xs and ys and zs and values):
            out.refused.append("a volume without x, y, z and value")
            return
        ux, uy, uz = (sorted({_clean(v) for v in a}) for a in (xs, ys, zs))
        at = {(_clean(a), _clean(b), _clean(c)): _clean(v) for a, b, c, v in zip(xs, ys, zs, values, strict=False)}
        flat = [at.get((x, y, z)) for z in uz for y in uy for x in ux]
        scene["grid"] = {"x": ux, "y": uy, "z": uz, "values": flat}
        scene["scalars"] = {_text(t.get("name")) or "value": {**({"unit": unit} if unit else {}),
                                                              "association": "point", "values": []}}
        iso = [v for v in (_num(t.get("isomin")), _num(t.get("isomax"))) if v is not None]
        if iso:
            doc.setdefault("encoding", {})["iso"] = sorted({_clean(v) for v in iso})
    doc["data"] = {"scene": scene}
    enc = doc.setdefault("encoding", {})
    cam = _camera(layout)
    if cam:
        enc["camera"] = cam
    if _num(t.get("opacity")) is not None and _num(t.get("opacity")) < 1:
        enc["opacity"] = _clean(t["opacity"])
    scale = t.get("colorscale")
    name = scale if isinstance(scale, str) else ("the author's" if scale else "")
    if name and name.lower() not in palettes.SEQUENTIAL:
        out.replaced.append(f"colour scale {name} (drawn {palettes.DEFAULT_SEQUENTIAL})")
    elif isinstance(name, str) and name.lower() in palettes.SEQUENTIAL and name.lower() != palettes.DEFAULT_SEQUENTIAL:
        enc["colormap"] = name.lower()
    if not enc:
        doc.pop("encoding")
    if t.get("name"):
        doc["_title"] = _text(t.get("name"))


def import_vtk_scene(scene: Mapping[str, Any], *, ref: str, title: str, chart_id: str | None = None,
                     camera: Mapping[str, float] | None = None) -> Imported:
    """A trame-vtk scene's polydata as a ``mesh3d`` spec.

    ``scene`` is the polydata a ``VtkLocalView`` ships to vtk.js, read on the
    server: ``{points: [[x,y,z],...], polys: [[i,j,k,...],...], point_data:
    {name: [...]}, cell_data: {name: [...]}, elements?: [...], length_unit?}``.
    The original is kept by reference and digest like any import.
    """
    out = Imported()
    points, polys = scene.get("points") or [], scene.get("polys") or []
    if not points or not polys:
        out.refused.append("the scene has no polygons; a VTK view of lines or a volume is not imported yet")
        out.fallback = None
        return out
    # VTK keeps points in single precision: 1.8733 comes back as 1.8732999563.
    # Read each as the shortest decimal that is that float32, which is the
    # number its author wrote.
    def single(v: Any) -> Any:
        n = _num(v)
        return None if n is None else _clean(float(f"{struct.unpack('<f', struct.pack('<f', n))[0]:.7g}"))

    data: dict[str, Any] = {"mesh": {"points": [[single(v) for v in p] for p in points],
                                     "cells": [[int(i) for i in c] for c in polys]}}
    scalars = {}
    for assoc, key in (("cell", "cell_data"), ("point", "point_data")):
        for name, values in (scene.get(key) or {}).items():
            scalars[name] = {"association": assoc, "values": [_clean(v) for v in values],
                             **({"unit": scene["units"][name]} if (scene.get("units") or {}).get(name) else {})}
    if scalars:
        data["scalars"] = scalars
    if scene.get("elements"):
        data["elements"] = [str(e) for e in scene["elements"]]
    if scene.get("length_unit"):
        data["length_unit"] = str(scene["length_unit"])
    doc: dict[str, Any] = {"schema_version": us.SCHEMA_VERSION, "kind": "mesh3d", "title": title,
                           "data": {"scene": data}}
    if chart_id:
        doc["id"] = chart_id
    if camera:
        doc["encoding"] = {"camera": dict(camera)}
    doc["source"] = {"type": "imported", "format": "trame", "figure": ref, "digest": us.figure_digest(scene),
                     "importer_version": IMPORTER_VERSION, "revision": 1}
    ordered = {k: doc[k] for k in us.DOCUMENT_KEYS if k in doc}
    problems = us.validate(ordered)
    if problems:
        out.refused.extend(problems)
        return out
    out.spec = us.parse(ordered)
    return out


def _icicle(doc: dict[str, Any], t: Mapping[str, Any], out: Imported) -> None:
    """An icicle trace as a flame graph's parent-id rows.

    ``branchvalues: "total"`` means a value includes its children; the default,
    ``"remainder"``, means it is the node's own share, which is what ``self``
    says in the spec.
    """
    doc["kind"] = "flame"
    labels = [strip_html(v) for v in (_decode(t.get("labels")) or [])]
    ids = _decode(t.get("ids")) or labels
    parents = _decode(t.get("parents")) or []
    values = _decode(t.get("values")) or [0] * len(labels)
    if not labels or len(parents) != len(labels) or len(ids) != len(labels):
        out.refused.append("the icicle's labels, ids and parents do not line up")
        return
    total = t.get("branchvalues") == "total"
    rows = []
    for i, label in enumerate(labels):
        row: dict[str, Any] = {"id": str(ids[i]), "name": label}
        p = parents[i]
        if p not in (None, ""):
            row["parent"] = str(p)
        v = _clean(values[i]) if i < len(values) else None
        if v is not None:
            row["value" if total else "self"] = v
        rows.append(row)
    tree: dict[str, Any] = {"rows": rows}
    unit = str(_meta(t).get("unit") or "")
    if unit:
        tree["unit"] = unit
    doc["data"] = {"tree": tree}
    tiling = t.get("tiling") or {}
    if tiling.get("orientation") == "h":
        out.replaced.append("horizontal tiling (drawn vertically)")
    if tiling.get("flip") in ("y", "x+y"):
        doc["encoding"] = {"direction": "flame"}
    else:
        doc["encoding"] = {"direction": "icicle"}
    if (t.get("marker") or {}).get("colors") or (t.get("marker") or {}).get("colorscale"):
        out.replaced.append("frame colours")
    if t.get("name"):
        doc["_title"] = _text(t.get("name"))


def _heatmap(doc: dict[str, Any], t: Mapping[str, Any], layout: Mapping[str, Any], out: Imported) -> None:
    doc["kind"] = "heatmap"
    z = _decode(t.get("z"))
    if not z or not isinstance(z[0], list):
        out.refused.append("the heatmap has no 2-D z")
        return
    fld: dict[str, Any] = {}
    unit = str(_meta(t).get("unit") or "") or _text((t.get("colorbar") or {}).get("title"))
    m = _TITLE_UNIT.search(unit)
    if m:
        unit = m.group(1).strip()
    if unit:
        fld["unit"] = unit
    if t.get("name"):
        fld["name"] = _text(t.get("name"))
    for axis in ("x", "y"):
        vals = _decode(t.get(axis))
        if vals is not None:
            fld[axis] = [_x(v) for v in vals]
    fld["z"] = [[_clean(v) for v in row] for row in z]
    for k in ("zmin", "zmax"):
        if _num(t.get(k)) is not None:
            fld[k] = _clean(t[k])
    doc["data"] = {"field": fld}
    scale = t.get("colorscale")
    name = scale if isinstance(scale, str) else ("the author's" if scale else "")
    if name and name.lower() not in palettes.SEQUENTIAL:
        out.replaced.append(f"colour scale {name} (drawn {palettes.DEFAULT_SEQUENTIAL})")
    enc: dict[str, Any] = {}
    if isinstance(name, str) and name.lower() in palettes.SEQUENTIAL and name.lower() != palettes.DEFAULT_SEQUENTIAL:
        enc["colormap"] = name.lower()
    if (layout.get("yaxis") or {}).get("scaleanchor") == "x":
        enc["aspect"] = "equal"
    if enc:
        doc["encoding"] = enc
    axes = {}
    for axis in ("x", "y"):
        title = _text((layout.get(f"{axis}axis") or {}).get("title"))
        if title:
            axes[axis] = {"title": title}
    if axes:
        doc["axes"] = axes


def _indicators(doc: dict[str, Any], data: list[Mapping[str, Any]], out: Imported) -> None:
    rows = []
    for t in data:
        mode = str(t.get("mode") or "number")
        gauge = t.get("gauge") or {}
        number = t.get("number") or {}
        unit = str(_meta(t).get("unit") or "") or strip_html(number.get("suffix") or "").strip()
        row: dict[str, Any] = {"value": _clean(t.get("value"))}
        if unit:
            row["unit"] = unit
        label = _text(t.get("title"))
        if label:
            row["label"] = label
        ref = (t.get("delta") or {}).get("reference")
        if "delta" in mode and _num(ref) is not None and _num(t.get("value")) is not None:
            row["delta"] = _clean(float(t["value"]) - float(ref))
        rng = (gauge.get("axis") or {}).get("range")
        if rng and len(rng) == 2 and _num(rng[0]) is not None and _num(rng[1]) is not None:
            row["min"], row["max"] = _clean(rng[0]), _clean(rng[1])
        thr = (gauge.get("threshold") or {}).get("value")
        if _num(thr) is not None:
            row["target"] = _clean(thr)
        steps = gauge.get("steps") or []
        edges = sorted({_clean(r) for st in steps if isinstance(st, Mapping)
                        for r in (st.get("range") or [])[1:2] if _num(r) is not None})
        if edges and "max" in row:
            edges = [e for e in edges if e < row["max"]]
        if edges:
            row["ranges"] = edges
        if gauge.get("bar", {}).get("color") or steps:
            out.replaced.append("gauge colours")
        rows.append((mode, gauge.get("shape"), row, t))
    if len(rows) == 1:
        mode, shape, row, t = rows[0]
        if "gauge" in mode:
            doc["kind"] = "bullet" if shape == "bullet" else "gauge"
        else:
            doc["kind"] = "kpi"
        doc["data"] = {"value": row}
        doc["_title"] = row.get("label", "")
        return
    if any("gauge" in mode for mode, *_ in rows):
        out.refused.append("several gauges in one figure; import each as its own chart")
        return
    doc["kind"] = "kpi"
    series = []
    for i, (_m, _s, row, t) in enumerate(rows):
        s = {"name": row.get("label") or _text(t.get("name")) or f"value {i + 1}"}
        if row.get("unit"):
            s["unit"] = row["unit"]
        s["y"] = [row.get("value")]
        series.append(s)
    doc["data"] = {"series": series}


# ---------------------------------------------------------------------------
# Widgets: update menus, sliders and frames.
# ---------------------------------------------------------------------------


def _visible_groups(menu: Mapping[str, Any], n: int) -> list[tuple[str, set[int]]] | None:
    groups = []
    for b in menu.get("buttons") or []:
        args = b.get("args") or []
        vis = args[0].get("visible") if args and isinstance(args[0], Mapping) else None
        if not isinstance(vis, list) or len(vis) != n:
            return None
        groups.append((_text(b.get("label")), {i for i, v in enumerate(vis) if v is True}))
    return groups


def _menus(doc: dict[str, Any], figure: Mapping[str, Any], data: list[Mapping[str, Any]],
           layout: Mapping[str, Any], out: Imported) -> None:
    controls: list[dict[str, Any]] = []
    series = (doc.get("data") or {}).get("series") or []
    pairs, _ = _panels(data, layout)
    for mi, menu in enumerate(layout.get("updatemenus") or []):
        if not isinstance(menu, Mapping):
            continue
        buttons = [b for b in menu.get("buttons") or [] if isinstance(b, Mapping)]
        methods = {b.get("method") for b in buttons}
        active = menu.get("active", 0) if isinstance(menu.get("active"), int) else 0
        if methods == {"restyle"}:
            groups = _visible_groups(menu, len(data))
            if not groups or len(series) != len(data):
                out.refused.append(f"update menu {mi + 1} restyles something other than which traces show")
                continue
            for label, members in groups:
                for i in members:
                    series[i]["group"] = label
            # Series that every button shows belong to no group.
            everywhere = set.intersection(*[m for _, m in groups]) if groups else set()
            for i in everywhere:
                series[i].pop("group", None)
            options = [{"value": label, "label": label} for label, _ in groups]
            ctype = "dropdown" if menu.get("type") == "dropdown" else "segmented"
            controls.append({"id": f"menu-{mi + 1}", "type": ctype,
                             "label": _menu_label(options, " / ".join(o["label"] for o in options)),
                             "bind": {"to": "group"}, "options": options,
                             "value": options[min(active, len(options) - 1)]["value"]})
        elif methods == {"relayout"}:
            # Each button is a state of the annotations/shapes; the extra marks
            # the richest state adds are one layer a toggle shows or hides.
            states = []
            for b in buttons:
                args = b.get("args") or [{}]
                a0 = args[0] if isinstance(args[0], Mapping) else {}
                if set(a0) - {"annotations", "shapes"}:
                    states = None
                    break
                states.append((b, a0))
            if not states:
                out.refused.append(f"update menu {mi + 1} relayouts more than annotations and shapes")
                continue
            richest = max(states, key=lambda s: len(s[1].get("annotations") or []) + len(s[1].get("shapes") or []))
            poorest = min(states, key=lambda s: len(s[1].get("annotations") or []) + len(s[1].get("shapes") or []))
            base_ann = len(poorest[1].get("annotations") or [])
            extra_ann = (richest[1].get("annotations") or [])[base_ann:]
            layer = _slug(_menu_label([{"label": _text(b.get("label"))} for b in buttons], "layer"), f"layer-{mi + 1}")
            extra = _marks(extra_ann, richest[1].get("shapes") or [], pairs, layout, layer=layer)
            if not extra:
                continue
            doc.setdefault("marks", []).extend(extra)
            on = buttons.index(richest[0])
            controls.append({"id": layer, "type": "toggle",
                             "label": _menu_label([{"label": _text(b.get("label"))} for b in buttons], "Marks"),
                             "bind": {"to": "layer", "key": layer}, "value": active == on})
        elif methods <= {"animate"}:
            if figure.get("frames"):
                controls.append({"id": "play", "type": "playback", "label": "Play",
                                 "bind": {"to": "frame"}, "interval_ms": _interval(buttons)})
        else:
            out.refused.append(f"update menu {mi + 1} uses {', '.join(sorted(str(m) for m in methods))}, "
                               "which changes more than one thing at once")
    frames = figure.get("frames") or []
    if frames:
        _frames(doc, frames, data, layout, out)
        slider = next((s for s in layout.get("sliders") or [] if isinstance(s, Mapping)), None)
        label = _text((slider or {}).get("currentvalue", {}).get("prefix")).rstrip(": =") if slider else ""
        controls.insert(0, {"id": "frame", "type": "slider", "label": label or "Frame",
                            "bind": {"to": "frame"}})
    elif layout.get("sliders"):
        out.refused.append("a slider without animation frames; it restyles the figure in place")
    if controls:
        doc["controls"] = controls


def _interval(buttons: list[Mapping[str, Any]]) -> int:
    for b in buttons:
        args = b.get("args") or []
        if len(args) > 1 and isinstance(args[1], Mapping):
            d = (args[1].get("frame") or {}).get("duration")
            if _num(d):
                return int(d)
    return 500


def _menu_label(options: list[Mapping[str, Any]], fallback: str) -> str:
    labels = [str(o.get("label") or "") for o in options]
    words = [lab.split() for lab in labels if lab]
    if len(words) > 1:
        common = []
        for parts in zip(*words, strict=False):
            if len(set(parts)) == 1:
                common.append(parts[0])
            else:
                break
        if common:
            return " ".join(common)
    return fallback


def _frames(doc: dict[str, Any], frames: list[Any], data: list[Mapping[str, Any]],
            layout: Mapping[str, Any], out: Imported) -> None:
    items = []
    for i, fr in enumerate(frames):
        if not isinstance(fr, Mapping):
            continue
        traces = list(fr.get("data") or [])
        merged = [dict(base, **(traces[k] if k < len(traces) and isinstance(traces[k], Mapping) else {}))
                  for k, base in enumerate(data)]
        sub = Imported()
        tmp: dict[str, Any] = {"schema_version": us.SCHEMA_VERSION}
        if doc["kind"] == "heatmap":
            _heatmap(tmp, merged[0], layout, sub)
        elif doc["kind"] in ("kpi", "gauge", "bullet"):
            _indicators(tmp, merged, sub)
        elif doc["kind"] in ("pie", "donut"):
            _pie(tmp, merged[0], sub)
        else:
            _cartesian(tmp, _FAMILY[str(merged[0].get("type") or "scatter")], merged, layout, sub)
        if sub.refused:
            out.refused.extend(f"frame {i + 1}: {r}" for r in sub.refused)
            return
        name = fr.get("name", str(i))
        value = _clean(name) if _num(name) is not None else str(name)
        items.append({"value": value, "label": str(name), "data": tmp["data"]})
    doc["data"] = {"frames": {"key": "frame", "items": items}}


def _as_query(doc: dict[str, Any], data: list[Mapping[str, Any]], site: str) -> None:
    series = (doc.get("data") or {}).get("series") or []
    channels = [str(_meta(t).get("channel") or "") for t in data]
    if not series or not all(channels) or doc.get("kind") not in ("line", "step", "area", "scatter", "bar"):
        return
    xs = [x for s in series for x in s.get("x") or [] if isinstance(x, str)]
    q: dict[str, Any] = {"site": site, "channels": channels}
    if xs:
        q["window"] = {"from": min(xs), "to": max(xs)}
    doc["data"] = {"query": q}


# ---------------------------------------------------------------------------
# Re-import, and Trame.
# ---------------------------------------------------------------------------


def reimport(previous: Mapping[str, Any], figure: Mapping[str, Any], *, ref: str | None = None) -> Imported:
    """Read a new version of an imported figure into the SAME chart.

    The id stays, the revision goes up, and the original reference is kept
    unless a new one is named. A re-import that is refused leaves the previous
    spec standing; the caller keeps it and shows the refusal.
    """
    src = previous.get("source") or {}
    if src.get("type") != "imported":
        raise ValueError("only an imported chart can be re-imported")
    res = import_figure(figure, ref=ref or src["figure"], chart_id=previous.get("id"),
                        fmt=src.get("format", "plotly"), revision=int(src.get("revision", 1)) + 1)
    return res


#: Trame (Vuetify) widgets and the control each becomes.
_TRAME_WIDGETS = {
    "VSlider": "slider", "VRangeSlider": "range", "VSelect": "dropdown", "VAutocomplete": "dropdown",
    "VSwitch": "toggle", "VCheckbox": "toggle", "VBtnToggle": "segmented", "VRadioGroup": "segmented",
}


def trame_controls(widgets: Sequence[Mapping[str, Any]], state: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Trame widget declarations -> ChartSpec controls, and what was not mapped.

    ``widgets`` is the list :func:`walk_trame_layout` produces (or a hand-made
    one): ``{"type": "VSlider", "v_model": "z", "label": ..., "min", "max",
    "step", "items"}``. A control binds the state variable it drives as a
    ``param``; the exporter that sampled the figure per value rebinds it to
    ``frame`` (:func:`import_trame`).
    """
    controls, skipped = [], []
    for w in widgets:
        kind = _TRAME_WIDGETS.get(str(w.get("type")))
        key = str(w.get("v_model") or "")
        if not kind or not key:
            skipped.append(f"{w.get('type')} ({key or 'no v_model'})")
            continue
        c: dict[str, Any] = {"id": _slug(key, "control"), "type": kind,
                             "label": strip_html(w.get("label") or key), "bind": {"to": "param", "key": key}}
        items = w.get("items")
        if kind in ("dropdown", "segmented"):
            if not items:
                skipped.append(f"{w.get('type')} {key}: no items")
                continue
            c["options"] = [({"value": i.get("value"), "label": strip_html(i.get("title") or i.get("value"))}
                             if isinstance(i, Mapping) else {"value": i, "label": str(i)}) for i in items]
        if kind in ("slider", "range"):
            for k in ("min", "max", "step"):
                if _num(w.get(k)) is not None:
                    c[k] = _clean(w[k])
        if key in state:
            c["value"] = state[key]
        controls.append(c)
    return controls, skipped


def import_trame(
    figures: Mapping[str, Mapping[str, Any]],
    *,
    widgets: Sequence[Mapping[str, Any]] = (),
    state: Mapping[str, Any] | None = None,
    sweeps: Mapping[str, Mapping[str, Any]] | None = None,
    ref_prefix: str = "trame://state",
) -> dict[str, Imported]:
    """Every figure of a Trame app, as specs; its widgets as their controls.

    ``figures`` is ``{state_key: plotly_json}`` as the app's server state holds
    them. ``sweeps`` is ``{state_key: {"var": "z", "values": {value: figure}}}``
    for a figure the app recomputes when a widget moves: the exporter sampled it
    at each value, and the figure becomes frames with the widget's control
    bound to them. That is how a Trame slider keeps working with no Trame
    server behind the chart.
    """
    state = state or {}
    controls, _skipped = trame_controls(widgets, state)
    by_var = {c["bind"]["key"]: c for c in controls}
    out: dict[str, Imported] = {}
    for key, fig in figures.items():
        sweep = (sweeps or {}).get(key)
        if sweep:
            var = sweep["var"]
            values = sweep["values"]
            frames = [{"name": v, "data": f.get("data")} for v, f in values.items()]
            merged = dict(fig)
            merged["frames"] = frames
            res = import_figure(merged, ref=f"{ref_prefix}/{key}", chart_id=_slug(key, "chart"), fmt="trame")
            if res.native:
                c = copy.deepcopy(by_var.get(var) or {"id": _slug(var, "frame"), "type": "slider",
                                                      "label": var})
                c["bind"] = {"to": "frame"}
                c.pop("min", None)
                c.pop("max", None)
                c.pop("step", None)
                c.pop("options", None)
                items = res.spec["data"]["frames"]["items"]
                frame_values = [it["value"] for it in items]
                if c["type"] in ("dropdown", "segmented"):
                    c["options"] = [{"value": it["value"], "label": it.get("label") or str(it["value"])}
                                    for it in items]
                if c.get("value") not in frame_values:
                    c["value"] = frame_values[0]
                rest = [x for x in res.spec.get("controls") or [] if x["id"] != "frame"]
                spec = dict(res.spec)
                spec["controls"] = [c, *rest]
                problems = us.validate(spec)
                if problems:
                    res.refused.extend(problems)
                    res.spec = None
                else:
                    res.spec = us.parse(spec)
        else:
            res = import_figure(fig, ref=f"{ref_prefix}/{key}", chart_id=_slug(key, "chart"), fmt="trame")
        out[key] = res
    return out


def walk_trame_layout(node: Any, state: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Widget declarations from a built Trame layout, by duck typing.

    A trame element keeps ``_elem_name`` and ``_attributes`` as the Vue
    attribute strings it will render (``'label="Axial"'``, ``':items="opts"'``,
    ``'v-model="z"'``). A bound (``:``) attribute names state, and is read from
    *state*. Anything unrecognised is skipped. Kept duck-typed so the importer
    never imports trame.
    """
    state = state or {}
    found: list[dict[str, Any]] = []
    attr = re.compile(r'^(:?)([\w-]+)="(.*)"$', re.S)

    def value_of(raw: str) -> Any:
        m = attr.match(raw.strip())
        if not m:
            return None
        bound, _name, text = m.groups()
        if bound:
            return state.get(text, None)
        n = _num(text)
        return _clean(n) if n is not None else text

    def visit(el: Any) -> None:
        name = str(getattr(el, "_elem_name", "") or "")
        if name in _TRAME_WIDGETS:
            attrs = dict(getattr(el, "_attributes", {}) or {})
            model = attrs.get("v_model", "")
            m = attr.match(model.strip()) if model else None
            w: dict[str, Any] = {"type": name, "v_model": m.group(3) if m else None}
            for k in ("label", "min", "max", "step", "items"):
                if k in attrs:
                    v = value_of(attrs[k])
                    if v is not None:
                        w[k] = v
            found.append(w)
        for child in getattr(el, "_children", None) or []:
            visit(child)

    visit(node)
    return found


# ---------------------------------------------------------------------------
# The fallback: the author's figure, sanitised, in the platform's colours.
# ---------------------------------------------------------------------------

_SCRIPTY = re.compile(r"<\s*(script|iframe|object|embed)|javascript:|on[a-z]+\s*=", re.I)


def sanitize_figure(figure: Mapping[str, Any]) -> dict[str, Any]:
    """A copy with nothing executable and no markup in its text.

    A figure is data. What can carry code or a link out (``hovertemplate``
    HTML, ``customdata``, ``transforms``, ``meta`` beyond axiom's, link anchors
    in any text) is removed before it leaves the server, so the fallback lane
    in the browser is handed numbers and words only.
    """
    def scrub(value: Any, key: str = "") -> Any:
        if isinstance(value, Mapping):
            out = {}
            for k, v in value.items():
                if k in ("customdata", "transforms", "hovertemplate", "texttemplate", "hovertext",
                         "href", "link", "frames"):
                    continue
                if k == "meta" and not (isinstance(v, Mapping) and set(v) <= {"axiom"}):
                    continue
                out[k] = scrub(v, k)
            return out
        if isinstance(value, list):
            return [scrub(v, key) for v in value]
        if isinstance(value, str) and (key in ("text", "name", "title", "label", "prefix", "suffix")
                                       or _SCRIPTY.search(value)):
            return strip_html(value)
        return value

    clean = scrub({"data": figure.get("data") or [], "layout": figure.get("layout") or {}})
    return clean


#: The sequential scale a fallback field is drawn in.
SEQUENTIAL_SCALE = "Cividis"
PLOT_TOKENS = ("--axk-plot-paper", "--axk-plot-ink", "--axk-plot-muted", "--axk-plot-rule")


def theme_figure(figure: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of *figure* for the fallback renderer, coloured as the platform colours."""
    from .chart_colour import assign_colours

    data = [dict(t) for t in (figure.get("data") or []) if isinstance(t, Mapping)]
    layout = dict(figure.get("layout") or {})
    pairs: list[tuple[str, object]] = []
    for trace in data:
        yaxis = layout.get(_layout_key(str(trace.get("yaxis") or "y"))) or {}
        unit = str(_meta(trace).get("unit") or "") or _unit_from_title(yaxis)
        pairs.append((_text(trace.get("name")) or str(trace.get("type") or "trace"), unit))
    colours, _ = assign_colours(pairs) if pairs else ([], [])
    for trace, colour in zip(data, colours, strict=True):
        kind = str(trace.get("type") or "scatter")
        if kind in ("heatmap", "heatmapgl", "contour", "histogram2d", "surface", "histogram2dcontour"):
            trace["colorscale"] = SEQUENTIAL_SCALE
            continue
        for part in ("line", "marker"):
            d = dict(trace.get(part) or {})
            d["color"] = colour
            trace[part] = d
    layout.pop("template", None)
    layout.pop("colorway", None)
    layout["font"] = {"family": LABEL_FONT}
    meta = layout.get("meta") if isinstance(layout.get("meta"), Mapping) else {}
    layout["meta"] = {**meta, "axiom_themed": True}
    return {"data": data, "layout": layout}


def _layout_key(ref: str) -> str:
    return f"{ref[0]}axis{ref[1:]}"


__all__ = [
    "IMPORTER_VERSION", "Imported", "import_vtk_scene", "PLOT_TOKENS", "SEQUENTIAL_SCALE", "import_figure", "import_trame",
    "reimport", "sanitize_figure", "theme_figure", "trame_controls", "walk_trame_layout",
]
