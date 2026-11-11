# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A small kit for documentation diagrams: data in, 2x PNG out.

A diagram is Python: boxes on a grid, arrows between them. The kit writes an SVG,
renders it through headless Chrome at 2x, shrinks the PNG, and checks the layout.

    d = Diagram("flow", "A title", height=300, eyebrow="PRODUCT")
    a = d.node(36, 100, 160, 60, "Reads", "from the queue")
    b = d.node(320, 100, 160, 60, "Writes", tone="accent")
    d.arrow(a.r(), b.l(), "commits")
    render([d], out_dir)            # writes flow.png; raises on a layout fault

The look is one language in every repository that carries this file: IBM Plex,
a blue range for structure and flow, a 960 px canvas so type is the
same size in every image, and an opaque panel so a figure reads on a light or a
dark page. Only the small label over the title differs by product; pass it to
``configure``.

Needs Chrome (set CHROME to its path if it is not in the usual place) and Pillow.
IBM Plex comes from the pinned @fontsource packages once, then from a local cache.
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

WIDTH = 960
MARGIN = 36
SCALE = 2

INK, INK2, INK3 = "#1d1d1b", "#46453f", "#76746d"
PANEL, CARD, RULE = "#faf9f6", "#ffffff", "#e3e0d8"
SANS, MONO = "IBM Plex Sans", "IBM Plex Mono"

# Structure and flow are blue, from the saturated core to the bright halo. ``ramp``
# walks that range; a gradient earns its place on a sequence.
CORE, HALO, ACTION = "#193d9a", "#98c1ff", "#1b3fa6"
LINE = CORE

#: tone name -> (fill, strong colour). A status tone is for a status only, and a
#: status is always a glyph and a word, never colour alone.
TONES = {
    "plain": (CARD, "#bcc6d6"),
    "none": ("#f1efe9", "#5f6d78"),   # a neutral light fill, for what sits outside
    "blue": ("#e9eefa", ACTION),
    "core": ("#dfe7f8", CORE),
    "halo": ("#eaf2ff", "#2f5fb3"),
    "ok": ("#e3f2ee", "#0d7762"),     # nominal, and nothing else
    "alert": ("#fffbe3", "#333f48"),  # dark text with a gold marker, never red
}
_style = {"eyebrow": "", "identity": ACTION}


def configure(eyebrow: str = "", identity: str = ACTION) -> None:
    """Set the small label above every title and the one colour it is drawn in."""
    _style.update(eyebrow=eyebrow, identity=identity)


def ramp(t: float) -> str:
    """A colour on the blue range: 0 is the core, 1 the halo."""
    return _mix(HALO, CORE, max(0.0, min(1.0, t)))


def on(fill: str) -> str:
    """The ink to set on a fill: white if it reads at 4.5:1, otherwise dark."""
    def lum(c):
        v = [int(c[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        v = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in v]
        return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]
    return "#ffffff" if 1.05 / (lum(fill) + 0.05) >= 4.5 else "#0e1424"


@dataclass(frozen=True)
class Pt:
    x: float
    y: float
    d: str = ""  # the way a line leaves this point: "h", "v" or "" for free


@dataclass
class Box:
    id: int
    x: float
    y: float
    w: float
    h: float
    tone: str = "plain"
    is_zone: bool = False

    def t(self, f: float = 0.5) -> Pt:
        return Pt(self.x + self.w * f, self.y, "v")

    def b(self, f: float = 0.5) -> Pt:
        return Pt(self.x + self.w * f, self.y + self.h, "v")

    def l(self, f: float = 0.5) -> Pt:  # noqa: E743
        return Pt(self.x, self.y + self.h * f, "h")

    def r(self, f: float = 0.5) -> Pt:
        return Pt(self.x + self.w, self.y + self.h * f, "h")

    def contains(self, x: float, y: float, pad: float = 0) -> bool:
        return (self.x - pad <= x <= self.x + self.w + pad
                and self.y - pad <= y <= self.y + self.h + pad)


@dataclass
class Arrow:
    pts: list[tuple[float, float]]
    a: int | None
    b: int | None
    label_ids: list[int] = field(default_factory=list)


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


class Diagram:
    def __init__(self, name: str, title: str, height: int, eyebrow: str | None = None):
        self.name, self.title, self.h = name, title, height
        self.back: list[str] = []   # zones
        self.lines: list[str] = []  # arrows sit over zones, under nodes
        self.front: list[str] = []  # nodes and labels
        self.boxes: list[Box] = []
        self.arrows: list[Arrow] = []
        self.owners: dict[int, int | None] = {}  # text id -> box id
        self._tid = 0
        self.head(eyebrow if eyebrow is not None else _style["eyebrow"])

    # -- text ------------------------------------------------------------------
    def text(self, x, y, s, size=13, weight=400, fill=INK, anchor="start", mono=False,
             owner=None, leading=1.32, spacing=0.0, target=None, italic=False, qa=True) -> int:
        tid = self._tid
        self._tid += 1
        if qa:
            self.owners[tid] = owner
        rows = s.split("\n")
        spans = "".join(
            f'<tspan x="{x}" dy="{0 if i == 0 else size * leading:.2f}">{_esc(r)}</tspan>'
            for i, r in enumerate(rows))
        extra = f' letter-spacing="{spacing}"' if spacing else ""
        extra += ' font-style="italic"' if italic else ""
        (self.front if target is None else target).append(
            f'<text id="t{tid}" data-qa="{1 if qa else 0}" x="{x}" y="{y}" font-family="{MONO if mono else SANS}" '
            f'font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}"{extra}>{spans}</text>')
        return tid

    def head(self, eyebrow: str) -> None:
        self.text(MARGIN, 37, eyebrow.upper(), 11, 500, _style["identity"], mono=True, spacing=1.1)
        self.text(MARGIN, 63, self.title, 21, 600, INK)

    # -- boxes -----------------------------------------------------------------
    def _box(self, x, y, w, h, tone, zone=False) -> Box:
        b = Box(len(self.boxes), x, y, w, h, tone, zone)
        self.boxes.append(b)
        return b

    def zone(self, x, y, w, h, label="", tone="none", label_at="tl") -> Box:
        b = self._box(x, y, w, h, tone, True)
        fill, strong = TONES[tone]
        self.back.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill}" '
                         f'fill-opacity=".62" stroke="{strong}" stroke-opacity=".28"/>')
        if label:
            lx, anchor = x + 14, "start"
            if label_at == "tr":
                lx, anchor = x + w - 14, "end"
            elif not isinstance(label_at, str):
                lx = label_at
            self.text(lx, y + 20, label.upper(), 11, 500, strong, anchor, mono=True, spacing=0.8, owner=b.id)
        return b

    def alert(self, x, y, w, h, title, size=13) -> Box:
        """An error or alarm: a word and a glyph on a gold marker, never colour alone."""
        b = self.node(x, y, w, h, title, tone="alert", size=size)
        cx, cy = x + w - 3, y + 3
        self.dot(cx, cy, 10, "#ffd600", "#333f48", 1.25)
        self.line(cx - 3.5, cy - 3.5, cx + 3.5, cy + 3.5, "#333f48", 2)
        self.line(cx - 3.5, cy + 3.5, cx + 3.5, cy - 3.5, "#333f48", 2)
        return b

    def node(self, x, y, w, h, title, sub=None, tone="plain", solid=False, mono=False,
             align="c", dash=False, size=14, sub_size=12.5, sub_mono=False, radius=8, fill=None) -> Box:
        b = self._box(x, y, w, h, tone)
        tint, strong = TONES[tone]
        if solid:
            fill = strong
        if fill:
            strong = fill
        ink, ink2 = (on(fill), on(fill)) if fill else (INK, INK2)
        fill = fill or tint
        dashes = ' stroke-dasharray="5 4"' if dash else ""
        self.front.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" '
                          f'stroke="{strong}" stroke-width="1.25"{dashes}/>')
        rows = title.split("\n")
        subs = sub.split("\n") if sub else []
        lead, sub_lead = size * 1.28, sub_size * 1.34
        block = lead * len(rows) + (sub_lead * len(subs) + 2 if subs else 0)
        top = y + (h - block) / 2
        tx = x + w / 2 if align == "c" else x + 14
        anchor = "middle" if align == "c" else "start"
        self.text(tx, round(top + size * 0.98, 1), title, size, 500 if mono else 600, ink,
                  anchor, mono, b.id)
        if subs:
            self.text(tx, round(top + lead * len(rows) + 2 + sub_size * 0.98, 1), sub, sub_size, 400,
                      ink2, anchor, sub_mono, b.id)
        return b

    def chip(self, x, y, w, h, s, tone="plain", mono=False, size=12) -> Box:
        return self.node(x, y, w, h, s, None, tone, mono=mono, size=size, radius=6)

    def pill(self, x, y, s, tone="plain", size=11.5, w=None, solid=False) -> Box:
        w = w or round(len(s) * size * 0.56 + 18)
        return self.node(x, y, w, 22, s, None, tone, size=size, radius=11, solid=solid)

    def rect(self, x, y, w, h, fill=CARD, stroke=RULE, rx=6, dash=False) -> None:
        dashes = ' stroke-dasharray="4 3"' if dash else ""
        self.front.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
                          f'stroke="{stroke}"{dashes}/>')

    def poly(self, pts, fill, stroke="none") -> None:
        s = " ".join(f"{x},{y}" for x, y in pts)
        self.front.append(f'<polygon points="{s}" fill="{fill}" stroke="{stroke}"/>')

    def dot(self, x, y, r=4, fill=INK, stroke="none", sw=1.25, target=None) -> None:
        (self.front if target is None else target).append(
            f'<circle cx="{x}" cy="{y}" r="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')

    def line(self, x1, y1, x2, y2, stroke=RULE, sw=1.25, dash=False) -> None:
        dashes = ' stroke-dasharray="5 4"' if dash else ""
        self.front.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" '
                          f'stroke-width="{sw}"{dashes}/>')

    def bar(self, x, y, s, n=1) -> None:
        """A small section label, used inside mock-ups."""
        self.text(x, y, s.upper(), 11, 500, INK3, mono=True, spacing=0.8, qa=True)

    # -- arrows ----------------------------------------------------------------
    @staticmethod
    def _route(pts, mid):
        out = [(pts[0].x, pts[0].y)]
        for p, q in zip(pts, pts[1:]):
            (x0, y0), (x1, y1) = out[-1], (q.x, q.y)
            if x0 != x1 and y0 != y1:
                if p.d == "v" and q.d == "v":
                    m = mid if mid is not None else (y0 + y1) / 2
                    out += [(x0, m), (x1, m)]
                elif p.d == "h" and q.d == "h":
                    m = mid if mid is not None else (x0 + x1) / 2
                    out += [(m, y0), (m, y1)]
                elif p.d == "v":
                    out.append((x0, y1))
                else:
                    out.append((x1, y0))
            out.append((x1, y1))
        return out

    def _owner_of(self, p: Pt) -> int | None:
        hits = [b for b in self.boxes if not b.is_zone and b.contains(p.x, p.y, 0.6)]
        return hits[-1].id if hits else None

    def arrow(self, a: Pt, b: Pt, label=None, via=(), mid=None, at=0.5, dash=False, tone=None,
              both=False, at_xy=None, anchor="middle", sw=1.5, head=True) -> Arrow:
        via = [Pt(*v) if not isinstance(v, Pt) else v for v in via]
        pts = self._route([a, *via, b], mid)
        colour = TONES[tone][1] if tone else LINE
        arr = Arrow(pts, self._owner_of(a), self._owner_of(b))
        self.arrows.append(arr)
        draw = list(pts)
        def trim(p0, p1):  # stop the line short of the arrowhead's tip
            dx, dy = p1[0] - p0[0], p1[1] - p0[1]
            n = max((dx * dx + dy * dy) ** 0.5, 1e-6)
            return (p1[0] - dx / n * 6, p1[1] - dy / n * 6)
        if head:
            draw[-1] = trim(draw[-2], draw[-1])
        if both:
            draw[0] = trim(draw[1], draw[0])
        d = f"M{draw[0][0]} {draw[0][1]}"
        for i in range(1, len(draw) - 1):
            (x0, y0), (x1, y1), (x2, y2) = draw[i - 1], draw[i], draw[i + 1]
            l0 = abs(x1 - x0) + abs(y1 - y0)
            l1 = abs(x2 - x1) + abs(y2 - y1)
            r = min(9, l0 / 2, l1 / 2)
            sx = x1 - (x1 - x0) / max(l0, 1) * r
            sy = y1 - (y1 - y0) / max(l0, 1) * r
            ex = x1 + (x2 - x1) / max(l1, 1) * r
            ey = y1 + (y2 - y1) / max(l1, 1) * r
            d += f" L{sx:.1f} {sy:.1f} Q{x1} {y1} {ex:.1f} {ey:.1f}"
        d += f" L{draw[-1][0]} {draw[-1][1]}"
        dashes = ' stroke-dasharray="6 4"' if dash else ""
        self.lines.append(f'<path d="{d}" fill="none" stroke="{colour}" stroke-width="{sw}"{dashes}/>')
        if head:
            self._head(pts[-2], pts[-1], colour)
        if both:
            self._head(pts[1], pts[0], colour)
        if label:
            self._label(arr, pts, label, at, at_xy, anchor)
        return arr

    def _head(self, p0, p1, colour):
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        n = max((dx * dx + dy * dy) ** 0.5, 1e-6)
        ux, uy = dx / n, dy / n
        x, y = p1
        a = (x - ux * 8.5 + uy * 4.2, y - uy * 8.5 - ux * 4.2)
        b = (x - ux * 8.5 - uy * 4.2, y - uy * 8.5 + ux * 4.2)
        self.lines.append(f'<polygon points="{x},{y} {a[0]:.1f},{a[1]:.1f} {b[0]:.1f},{b[1]:.1f}" fill="{colour}"/>')

    def _label(self, arr, pts, label, at, at_xy, anchor):
        if at_xy:
            x, y = at_xy
            knock = False
        else:
            lens = [abs(q[0] - p[0]) + abs(q[1] - p[1]) for p, q in zip(pts, pts[1:])]
            goal, run = sum(lens) * at, 0.0
            x, y = pts[-1]
            for (p, q), n in zip(zip(pts, pts[1:]), lens):
                if run + n >= goal:
                    f = (goal - run) / max(n, 1)
                    x, y = p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f
                    break
                run += n
            knock = True
        rows = label.count("\n") + 1
        top = y - (rows - 1) * 12 * 0.66 + 4 if knock else y
        bg = PANEL
        for z in self.boxes:
            if z.is_zone and z.contains(x, y):
                bg = _mix(TONES[z.tone][0], PANEL, 0.62)
        tid = self._tid
        if knock:
            self.front.append(f'<rect class="knock" data-for="t{tid}" fill="{bg}"/>')
        self.text(round(x, 1), round(top, 1), label, 12, 400, INK2, anchor, owner=None)
        if knock:  # a label set on its line may sit on it; one beside it may not
            arr.label_ids.append(tid)

    # -- output ----------------------------------------------------------------
    def svg(self) -> str:
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{self.h}" '
                f'viewBox="0 0 {WIDTH} {self.h}">'
                f'<defs><linearGradient id="range" gradientUnits="userSpaceOnUse" x1="{MARGIN}" x2="{WIDTH - MARGIN}" '
                f'y1="0" y2="0"><stop offset="0" stop-color="{CORE}"/><stop offset="1" stop-color="{HALO}"/>'
                f'</linearGradient></defs><rect x=".5" y=".5" width="{WIDTH - 1}" height="{self.h - 1}" fill="{PANEL}" stroke="{RULE}"/>'
                + "".join(self.back + self.lines + self.front) + "</svg>")


def _mix(fg: str, bg: str, a: float) -> str:
    f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * a + y * (1 - a)):02x}" for x, y in zip(f, b))


# -- fonts -----------------------------------------------------------------------
_FONTS = [("ibm-plex-sans", SANS, w) for w in (400, 500, 600)] + [("ibm-plex-mono", MONO, w) for w in (400, 500)]
_FONT_URL = "https://cdn.jsdelivr.net/npm/@fontsource/{pkg}@5.2.5/files/{pkg}-latin-{w}-normal.woff2"


def _font_css() -> str:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "doc-diagrams"
    cache.mkdir(parents=True, exist_ok=True)
    rules = []
    for pkg, family, w in _FONTS:
        f = cache / f"{pkg}-{w}.woff2"
        if not f.exists():
            with urllib.request.urlopen(_FONT_URL.format(pkg=pkg, w=w), timeout=30) as r:
                f.write_bytes(r.read())
        data = base64.b64encode(f.read_bytes()).decode()
        rules.append(f'@font-face{{font-family:"{family}";font-weight:{w};'
                     f'src:url(data:font/woff2;base64,{data}) format("woff2")}}')
    return "".join(rules)


_PAGE_JS = """
document.fonts.ready.then(()=>{
  document.querySelectorAll('rect.knock').forEach(r=>{
    const b=document.getElementById(r.dataset.for).getBBox();
    r.setAttribute('x',b.x-5);r.setAttribute('y',b.y-1.5);
    r.setAttribute('width',b.width+10);r.setAttribute('height',b.height+3);});
  const out={};
  document.querySelectorAll('text[data-qa="1"]').forEach(t=>{
    const b=t.getBBox();out[t.id.slice(1)]=[b.x,b.y,b.width,b.height,t.textContent];});
  const p=document.createElement('pre');p.id='qa';p.textContent=JSON.stringify(out);
  document.body.appendChild(p);p.style.display='none';
  document.title='done';
});
"""


def _page(d: Diagram, css: str) -> str:
    return (f"<!doctype html><meta charset=utf-8><style>{css}html,body{{margin:0;padding:0;"
            f"background:{PANEL}}}svg{{display:block}}</style>{d.svg()}<script>{_PAGE_JS}</script>")


def _chrome() -> str:
    for c in (os.environ.get("CHROME"), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              shutil.which("google-chrome"), shutil.which("chromium")):
        if c and Path(c).exists():
            return c
    raise SystemExit("Chrome not found: set CHROME to its path")


def _run(args: list[str], profile: str) -> str:
    cmd = [_chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
           f"--user-data-dir={profile}", "--virtual-time-budget=3000", *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=90).stdout


# -- layout checks ---------------------------------------------------------------
def _overlap(a, b, pad=0.0) -> bool:
    return (a[0] < b[0] + b[2] + pad and b[0] < a[0] + a[2] + pad
            and a[1] < b[1] + b[3] + pad and b[1] < a[1] + a[3] + pad)


def _seg_hits_rect(p, q, r, pad=0.0) -> bool:
    x0, x1 = sorted((p[0], q[0]))
    y0, y1 = sorted((p[1], q[1]))
    return x0 < r[0] + r[2] + pad and x1 > r[0] - pad and y0 < r[1] + r[3] + pad and y1 > r[1] - pad


def check(d: Diagram, texts: dict[int, list]) -> list[str]:
    """Return the faults a reader would see: clipping, overlap, crossing lines."""
    faults: list[str] = []
    names = {i: t[4] for i, t in texts.items()}
    rects = {i: t[:4] for i, t in texts.items()}
    nodes = [b for b in d.boxes if not b.is_zone]
    for i, r in rects.items():
        if r[0] < 14 or r[1] < 14 or r[0] + r[2] > WIDTH - 14 or r[1] + r[3] > d.h - 10:
            faults.append(f"off canvas: {names[i]!r}")
        own = d.owners.get(i)
        if own is not None:
            b = d.boxes[own]
            if r[0] < b.x + 5 or r[0] + r[2] > b.x + b.w - 5 or r[1] < b.y + 1 or r[1] + r[3] > b.y + b.h - 1:
                faults.append(f"text outside its box: {names[i]!r}")
        else:
            for b in nodes:
                if _overlap(r, (b.x, b.y, b.w, b.h), 2):
                    faults.append(f"text on a box: {names[i]!r}")
    ids = sorted(rects)
    for n, i in enumerate(ids):
        for j in ids[n + 1:]:
            ri, rj = rects[i], rects[j]  # a text box is taller than its ink, so y is forgiven
            if _overlap((ri[0], ri[1] + 3, ri[2], ri[3] - 6), (rj[0], rj[1] + 3, rj[2], rj[3] - 6), 2):
                faults.append(f"text overlap: {names[i]!r} / {names[j]!r}")
    for n, a in enumerate(nodes):
        for b in nodes[n + 1:]:
            if _overlap((a.x, a.y, a.w, a.h), (b.x, b.y, b.w, b.h), 2):
                faults.append(f"boxes touch: {a.id} / {b.id}")
    for n, ar in enumerate(d.arrows):
        segs = list(zip(ar.pts, ar.pts[1:]))
        tag = f"arrow {ar.pts[0]}->{ar.pts[-1]}"
        for p, q in segs:
            for b in nodes:
                if b.id in (ar.a, ar.b):
                    continue
                if _seg_hits_rect(p, q, (b.x, b.y, b.w, b.h), 3):
                    faults.append(f"{tag} runs through box {b.id}")
            for i, r in rects.items():
                if i in ar.label_ids:
                    continue
                own = d.owners.get(i)
                if own is not None and own in (ar.a, ar.b):
                    continue
                if _seg_hits_rect(p, q, r, 2):
                    faults.append(f"{tag} runs through text {names[i]!r}")
        for other in d.arrows[n + 1:]:
            for p, q in segs:
                for u, v in zip(other.pts, other.pts[1:]):
                    hp, vp = (p, q) if p[1] == q[1] else (u, v)
                    hq, vq = (u, v) if p[1] == q[1] else (p, q)
                    if hp[1] != vp[1] or hq[0] != vq[0]:
                        continue
                    if (min(hp[0], vp[0]) < hq[0] < max(hp[0], vp[0])
                            and min(hq[1], vq[1]) < hp[1] < max(hq[1], vq[1])):
                        faults.append(f"lines cross at ({hq[0]}, {hp[1]})")
    return sorted(set(faults))


def _shrink(path: Path) -> None:
    """Reduce to 256 colours: flat fills and text compress hard with no visible change.

    The palette keeps the most frequent colours exactly, and a median cut covers the
    rest. A plain quantize starves a colour used on few pixels (it browned the label
    over the title) or merges near neighbours (white into the panel).
    """
    img = Image.open(path).convert("RGB")
    flats = [c for _, c in sorted(img.getcolors(1 << 24), key=lambda n: -n[0])[:96]]
    rest = img.quantize(colors=160, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    palette = [v for c in flats for v in c] + rest.getpalette()[:480]
    table = [tuple(palette[i:i + 3]) for i in range(0, len(palette), 3)]
    index = {}
    def nearest(c):  # exact when the colour is in the palette; Pillow's own mapping is not
        if c not in index:
            index[c] = min(range(len(table)), key=lambda i: sum((u - v) ** 2 for u, v in zip(c, table[i])))
        return index[c]
    raw = img.tobytes()
    out = Image.frombytes("P", img.size, bytes(nearest(c) for c in zip(raw[0::3], raw[1::3], raw[2::3])))
    out.putpalette(palette)
    out.save(path, optimize=True)


def render(diagrams: list[Diagram], out: Path, only: set[str] | None = None, strict: bool = True) -> None:
    out.mkdir(parents=True, exist_ok=True)
    css = _font_css()
    failed: dict[str, list[str]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for d in diagrams:
            if only and d.name not in only:
                continue
            page = Path(tmp) / f"{d.name}.html"
            page.write_text(_page(d, css), encoding="utf-8")
            dom = _run(["--dump-dom", page.as_uri()], tmp)
            m = re.search(r'<pre id="qa"[^>]*>(.*?)</pre>', dom, re.S)
            if not m:
                raise SystemExit(f"{d.name}: the page did not finish rendering")
            data = json.loads(html.unescape(m.group(1)))
            faults = check(d, {int(k): v for k, v in data.items()})
            if faults:
                failed[d.name] = faults
            png = out / f"{d.name}.png"
            _run([f"--screenshot={png}", f"--window-size={WIDTH},{d.h}",
                  f"--force-device-scale-factor={SCALE}", page.as_uri()], tmp)
            with Image.open(png) as im:
                if im.size != (WIDTH * SCALE, d.h * SCALE):
                    raise SystemExit(f"{d.name}: rendered {im.size}, wanted {(WIDTH * SCALE, d.h * SCALE)}")
            _shrink(png)
            print(f"{png.name}: {png.stat().st_size // 1024} KB, {len(faults)} layout fault(s)")
    for name, faults in failed.items():
        print(f"\n{name}:")
        for f in faults:
            print(f"  {f}")
    if failed and strict:
        raise SystemExit("layout faults: fix the diagram, do not ship it")
