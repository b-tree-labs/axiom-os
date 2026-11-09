# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The one palette registry: every named colour progression a chart may use.

Three sorts, because they answer three different questions:

- **categorical** — *which one*: slices of a pie, bars in a group, series with
  no unit to colour them by. Hues far apart, in a fixed order, so slice three
  is the same colour in every pie.
- **sequential** — *how much*: a heatmap, a field. Light-to-dark or
  dark-to-light in one perceptual direction. A rainbow is not sequential: its
  bright bands read as structure the data does not have, and it fails for the
  common colour-vision deficiencies. The default is Cividis, designed to be
  read identically by people with and without red-green deficiency.
- **diverging** — *which side of a midpoint*: a residual, a difference.

Series that DO carry a unit are not coloured from here: :mod:`.chart_colour`
gives them the hue of what they measure. This registry covers everything a
unit cannot decide, and the harness palettes the Charts page offered (blues,
earth, rainbow, classic, ink) now live here rather than in a dev service, so a
lane, the renderer and the browser agree on what "classic" means.
"""

from __future__ import annotations


from .chart_colour import _channels, _hex, _to_linear, lab_to_rgb, rgb_to_lab

#: The papers a series is drawn on: the light figure and the dark panel. A line
#: or a label in a series colour must read on both, because the same figure is
#: shown in either theme and the colour is not allowed to change with it.
PAPERS = {"light": "#ffffff", "dark": "#1b1f23"}
#: WCAG non-text contrast for a line, and what a direct label needs at its size.
MIN_CONTRAST = 3.0


def luminance(colour: str) -> float:
    r, g, b = (_to_linear(c) for c in _channels(colour))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(one: str, two: str) -> float:
    a, b = sorted((luminance(one), luminance(two)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def legible(colour: str, paper: str = "light", minimum: float = MIN_CONTRAST) -> str:
    """The same hue at the nearest lightness that reads on *paper*.

    Moves only CIELAB lightness, the least it has to; chroma eases only if the
    moved colour would fall outside sRGB. Hue stays, so a temperature is still
    the warm it was.
    """
    ground = PAPERS[paper]
    if contrast(colour, ground) >= minimum:
        return colour
    lightness, a, b = rgb_to_lab(_channels(colour))
    for step in range(1, 201):
        for sign in ((1,) if paper == "dark" else (-1,)):
            target = lightness + sign * step * 0.5
            if not 0 <= target <= 100:
                continue
            for shrink in (1.0, 0.85, 0.7, 0.55, 0.4):
                rgb = lab_to_rgb(target, a * shrink, b * shrink)
                if rgb is not None and contrast(_hex(rgb), ground) >= minimum:
                    return _hex(rgb)
    return colour


def series_colour(colour: str) -> str:
    """A series colour as the renderer writes it: legible in both themes.

    The light figure keeps the colour exactly (it is what is exported, and what
    the colour-vision checks are made on). Where that colour is too dark for
    the dark panel, the dark theme gets the same hue lifted until it reads,
    through CSS ``light-dark()``, which follows the page's ``color-scheme``.
    :func:`.chart_svg.flatten_theme` resolves it to the light colour on export.
    """
    if not colour.startswith("#") or len(colour) != 7:
        return colour
    twin = legible(colour, "dark")
    return colour if twin == colour else f"light-dark({colour},{twin})"


def _legible_all(colours: list[str]) -> list[str]:
    return [legible(c, "light") for c in colours]

#: Okabe-Ito order, the categorical default: distinguishable under every
#: common colour-vision deficiency, and the house set the renderer already uses.
AXIOM = ["#0072b2", "#e69f00", "#009e73", "#cc79a7", "#56b4e9", "#d55e00", "#5b4a8a", "#4d4119"]

_CATEGORICAL_AS_AUTHORED: dict[str, list[str]] = {
    "axiom": AXIOM,
    "classic": ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
                "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"],
    # "ink" leads with the neutral that reads as ink on either paper; pure
    # black vanished on the dark panel.
    "ink": ["#6b7280", "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
            "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"],
    "blues": ["#1d4ed8", "#2563eb", "#3b82f6", "#60a5fa", "#93c5fd", "#bfdbfe"],
    "earth": ["#7c3f16", "#a86b3c", "#c69c6d", "#6b7b3a", "#3f6212", "#8c6d4f"],
    "rainbow": ["#dc2626", "#ea580c", "#ca8a04", "#16a34a", "#2563eb", "#7c3aed"],
}

#: Every categorical palette, each colour moved (lightness only) until it reads
#: on the light paper; the dark theme gets its twin from :func:`series_colour`.
#: A test checks every entry on both papers.
CATEGORICAL: dict[str, list[str]] = {k: _legible_all(v) for k, v in _CATEGORICAL_AS_AUTHORED.items()}

#: Ten stops each; anything between is interpolated in CIELAB.
SEQUENTIAL: dict[str, list[str]] = {
    "cividis": ["#00224e", "#123570", "#3b496c", "#575d6d", "#707173",
                "#8a8678", "#a59c74", "#c3b369", "#e1cc55", "#fee838"],
    "viridis": ["#440154", "#482878", "#3e4989", "#31688e", "#26828e",
                "#1f9e89", "#35b779", "#6ece58", "#b5de2b", "#fde725"],
    "greys": ["#ffffff", "#f0f0f0", "#d9d9d9", "#bdbdbd", "#969696",
              "#737373", "#525252", "#363636", "#252525", "#000000"],
}

DIVERGING: dict[str, list[str]] = {
    "blue-orange": ["#0072b2", "#4a9bc9", "#94c4df", "#dfe9ef", "#f2dcc7",
                    "#e8ae7b", "#d97e35", "#d55e00"],
}

#: Frame fills for a flame graph coloured by name: the warm set flame graphs
#: are read in, light enough that dark labels read on every one of them in
#: either theme. A frame takes one by a hash of its name, never at random.
FLAME = ["#f2a65a", "#eec170", "#e7885c", "#f4b183", "#e3a857", "#ef9a6b", "#f6c28b", "#e99c4e",
         "#f0b46b", "#e48d6a", "#f3cf8a", "#eaa679"]

DEFAULT_CATEGORICAL = "axiom"
DEFAULT_SEQUENTIAL = "cividis"

#: Colour scales other tools name that are not sequential, and what replaces
#: them. Reported by an importer, never honoured.
RAINBOWS = frozenset({"jet", "turbo", "rainbow", "hsv", "portland", "electric", "picnic"})


def categorical(name: str | None = None) -> list[str]:
    """A categorical palette by name; the default when unnamed or unknown."""
    return list(CATEGORICAL.get(name or DEFAULT_CATEGORICAL) or CATEGORICAL[DEFAULT_CATEGORICAL])


def sequential_name(name: str | None) -> str:
    """The sequential scale to use for an asked-for one: never a rainbow."""
    key = (name or "").strip().lower()
    return key if key in SEQUENTIAL else DEFAULT_SEQUENTIAL


def sample(stops: list[str], t: float) -> str:
    """The colour at *t* (0..1) along *stops*, interpolated in CIELAB."""
    t = 0.0 if t != t else max(0.0, min(1.0, t))  # NaN is the floor, not a crash
    if len(stops) == 1:
        return stops[0]
    pos = t * (len(stops) - 1)
    i = min(int(pos), len(stops) - 2)
    frac = pos - i
    a = rgb_to_lab(_channels(stops[i]))
    b = rgb_to_lab(_channels(stops[i + 1]))
    lab = tuple(a[k] + (b[k] - a[k]) * frac for k in range(3))
    rgb = lab_to_rgb(*lab)
    if rgb is None:  # outside sRGB between two in-gamut stops: take the nearer
        return stops[i] if frac < 0.5 else stops[i + 1]
    return _hex(rgb)


def sequential(name: str | None, t: float) -> str:
    return sample(SEQUENTIAL[sequential_name(name)], t)


#: A 3D scene's colours must stand off its paper: a face at the low end of the
#: scale on a dark panel, or at the high end on white, vanishes into the
#: background. Each theme samples only the part of the scale that clears this.
SCENE_MIN_CONTRAST = 1.6


def scene_stops(name: str | None, theme: str = "light", n: int = 10) -> list[str]:
    """The sequential scale for a 3D scene on *theme*'s paper.

    The same scale, cut at whichever end would sink into the background: on
    the dark panel the darkest stops are left off, on white the palest. The
    order and hue are kept, so a value reads the same way in either theme.
    """
    stops = SEQUENTIAL[sequential_name(name)]
    paper = PAPERS[theme]
    # Trim whichever end sinks into the paper (a scale may run either way).
    lo, hi = 0.0, 1.0
    while hi - lo > 0.2 and contrast(sample(stops, lo), paper) < SCENE_MIN_CONTRAST:
        lo += 0.01
    while hi - lo > 0.2 and contrast(sample(stops, hi), paper) < SCENE_MIN_CONTRAST:
        hi -= 0.01
    return [sample(stops, lo + (hi - lo) * i / (n - 1)) for i in range(n)]


def registry() -> dict[str, dict[str, list[str]]]:
    """Every palette, as a lane serves it to a browser."""
    return {
        "categorical": {k: list(v) for k, v in CATEGORICAL.items()},
        "sequential": {k: list(v) for k, v in SEQUENTIAL.items()},
        "diverging": {k: list(v) for k, v in DIVERGING.items()},
    }


__all__ = [
    "AXIOM", "CATEGORICAL", "FLAME", "DEFAULT_CATEGORICAL", "DEFAULT_SEQUENTIAL", "DIVERGING",
    "MIN_CONTRAST", "PAPERS", "RAINBOWS", "SEQUENTIAL", "categorical", "contrast", "legible", "scene_stops", "series_colour", "registry", "sample", "sequential",
    "sequential_name",
]
