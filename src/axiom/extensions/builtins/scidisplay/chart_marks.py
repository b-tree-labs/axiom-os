# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What a chart says ABOUT its data, drawn on the plot: bands, rules, notes.

A limit, a setpoint, the moment a mode changed. These are claims a figure
makes beside its readings, and they were the commonest reason an imported
figure could not be drawn natively: the spec reserved ``marks`` and no renderer
drew one. This is the one place that draws them, for every renderer that has a
plot box and two scales, so a band in a time series and a band in a spectrum
are the same band.

Drawn UNDER the data, never over it: a band that hides a reading has
mislabelled the reading. Tones are tokens (``--axk-plot-band-<tone>``), so a
limit is the same warning colour on a light page and a dark one.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

#: A band's fill and a rule's stroke, per tone. Literals are the light figure.
TONE_LITERALS = {
    "neutral": "#6b7280",
    "info": "#0072b2",
    "warn": "#b45309",
    "alarm": "#c0392b",
    "ok": "#15803d",
}

_TAG = re.compile(r"<[^>]*>")


def strip_html(text: Any) -> str:
    """Text without markup. A label is words, never a fragment of a page."""
    plain = _TAG.sub("", str(text or ""))
    return html.unescape(plain).replace(" ", " ").strip()


def tone(name: str | None) -> str:
    key = name if name in TONE_LITERALS else "neutral"
    return f"var(--axk-plot-band-{key},{TONE_LITERALS[key]})"


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def visible(marks: Iterable[Mapping[str, Any]] | None, hidden: set[str] | None = None,
            panel: str | None = None) -> list[Mapping[str, Any]]:
    """The marks to draw: not in a hidden layer, and on this panel (or on all)."""
    hidden = hidden or set()
    out = []
    for m in marks or []:
        if m.get("layer") in hidden:
            continue
        if panel is not None and m.get("panel") not in (None, panel):
            continue
        if panel is None and m.get("panel") is not None:
            continue
        out.append(m)
    return out


def draw_under(
    marks: list[Mapping[str, Any]],
    *,
    box: tuple[float, float, float, float],
    x_of: Callable[[Any], float | None],
    y_of: Callable[[Any, str], float | None],
    label_font: str,
    size: float = 10.5,
) -> list[str]:
    """Bands and rules, as SVG, for under the data.

    ``box`` is ``(left, top, width, height)`` of the plot. ``x_of(value)`` and
    ``y_of(value, axis)`` place a data value, or return ``None`` when it cannot
    be placed (off a log axis, an unknown axis); such a mark is skipped, not
    drawn at the edge, because a limit drawn in the wrong place is worse than
    none.
    """
    left, top, width, height = box
    out: list[str] = []
    for m in marks:
        kind = m.get("type")
        axis = str(m.get("axis") or "y")
        colour = tone(m.get("tone"))
        label = strip_html(m.get("label") or m.get("text") or "")
        if kind == "band":
            if axis == "x":
                a, b = x_of(m.get("from")), x_of(m.get("to"))
                if a is None or b is None:
                    continue
                a, b = sorted((max(left, min(left + width, a)), max(left, min(left + width, b))))
                out.append(f'<rect class="axk-band" x="{a:.1f}" y="{top:.1f}" width="{b - a:.1f}" '
                           f'height="{height:.1f}" fill="{colour}" fill-opacity="0.12"/>')
                if label:
                    out.append(f'<text x="{a + 4:.1f}" y="{top + size + 2:.1f}" font-size="{size:.2f}" '
                               f'fill="{colour}" font-family="{label_font}">{_esc(label)}</text>')
            else:
                a, b = y_of(m.get("from"), axis), y_of(m.get("to"), axis)
                if a is None or b is None:
                    continue
                a, b = sorted((max(top, min(top + height, a)), max(top, min(top + height, b))))
                out.append(f'<rect class="axk-band" x="{left:.1f}" y="{a:.1f}" width="{width:.1f}" '
                           f'height="{b - a:.1f}" fill="{colour}" fill-opacity="0.12"/>')
                if label:
                    out.append(f'<text x="{left + width - 4:.1f}" y="{a + size + 1:.1f}" '
                               f'font-size="{size:.2f}" fill="{colour}" text-anchor="end" '
                               f'font-family="{label_font}">{_esc(label)}</text>')
        elif kind == "rule":
            if axis == "x":
                x = x_of(m.get("value"))
                if x is None or not (left - 0.5 <= x <= left + width + 0.5):
                    continue
                out.append(f'<line class="axk-rule" x1="{x:.1f}" y1="{top:.1f}" x2="{x:.1f}" '
                           f'y2="{top + height:.1f}" stroke="{colour}" stroke-width="1" '
                           f'stroke-dasharray="4 3"/>')
                if label:
                    # Read bottom-to-top beside the line, as an event label is.
                    out.append(f'<text x="{x + 3:.1f}" y="{top + height - 4:.1f}" font-size="{size:.2f}" '
                               f'fill="{colour}" font-family="{label_font}" '
                               f'transform="rotate(-90 {x + 3:.1f} {top + height - 4:.1f})">{_esc(label)}</text>')
            else:
                y = y_of(m.get("value"), axis)
                if y is None or not (top - 0.5 <= y <= top + height + 0.5):
                    continue
                out.append(f'<line class="axk-rule" x1="{left:.1f}" y1="{y:.1f}" x2="{left + width:.1f}" '
                           f'y2="{y:.1f}" stroke="{colour}" stroke-width="1.2" stroke-dasharray="6 3"/>')
                if label:
                    out.append(f'<text x="{left + width - 4:.1f}" y="{y - 4:.1f}" font-size="{size:.2f}" '
                               f'fill="{colour}" text-anchor="end" font-family="{label_font}">{_esc(label)}</text>')
    return out


def draw_over(
    marks: list[Mapping[str, Any]],
    *,
    box: tuple[float, float, float, float],
    x_of: Callable[[Any], float | None],
    y_of: Callable[[Any, str], float | None],
    label_font: str,
    ink: str,
    size: float = 11.0,
) -> list[str]:
    """Annotations: text at a data position, over the data, with a small dot."""
    left, top, width, height = box
    out: list[str] = []
    for m in marks:
        if m.get("type") != "annotation":
            continue
        text = strip_html(m.get("text"))
        if not text:
            continue
        x = x_of(m.get("x")) if m.get("x") is not None else left + width / 2
        y = y_of(m.get("y"), str(m.get("axis") or "y")) if m.get("y") is not None else top + size + 2
        if x is None or y is None:
            continue
        colour = tone(m.get("tone")) if m.get("tone") else ink
        anchor = "end" if x > left + width * 0.75 else "start"
        dx = -6 if anchor == "end" else 6
        if m.get("y") is not None:
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.6" fill="{colour}"/>')
        out.append(f'<text class="axk-annotation" x="{x + dx:.1f}" y="{y - 5:.1f}" font-size="{size:.2f}" '
                   f'fill="{colour}" text-anchor="{anchor}" font-family="{label_font}">{_esc(text)}</text>')
    return out


__all__ = ["TONE_LITERALS", "draw_over", "draw_under", "strip_html", "tone", "visible"]
