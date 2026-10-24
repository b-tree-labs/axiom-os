# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0
"""Colour-vision simulation, so a palette claim can be CHECKED.

The palette carried a comment saying it was "checked for deuteranopia
separation". It was not. The third series colour and the reserved model colour
sat 4.9 apart in Lab under deuteranopia, which for two thin lines is the same
colour — and the model colour is the one reservation the whole scheme rests on.
A claim in a comment is not a check, so the check lives here and a test runs it.

Viénot, Brettel and Mollon (1999) for the dichromacies, CIE76 ``dE`` in Lab for
the distance, and the WCAG relative-luminance ratio for contrast on paper.
CIE76 overstates differences among saturated blues; at the magnitudes these
thresholds sit at that does not change an answer, and being a fixed rule it
cannot drift the way an eye can.
"""

from __future__ import annotations

import itertools
import math

#: Red-green deficiency is about 8% of men. Tritanopia is about 0.01%, and
#: separating it as well costs the other two, so it is measured and reported
#: rather than gated.
DICHROMACIES = ("deuteranopia", "protanopia", "tritanopia")
VISIONS = ("normal", *DICHROMACIES)

_RGB_LMS = (
    (17.8824, 43.5161, 4.11935),
    (3.45565, 27.1554, 3.86714),
    (0.0299566, 0.184309, 1.46709),
)
_LMS_RGB = (
    (0.080944, -0.130504, 0.116721),
    (-0.0102485, 0.0540194, -0.113615),
    (-0.000365294, -0.00412163, 0.693513),
)
_COLLAPSE = {
    "protanopia": ((0, 2.02344, -2.52581), (0, 1, 0), (0, 0, 1)),
    "deuteranopia": ((1, 0, 0), (0.494207, 0, 1.24827), (0, 0, 1)),
    "tritanopia": ((1, 0, 0), (0, 1, 0), (-0.395913, 0.801109, 0)),
}

WHITE = (1.0, 1.0, 1.0)


def _to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _to_srgb(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def rgb(colour: str) -> tuple[float, float, float]:
    """An ``#rrggbb`` string as three channels in 0..1."""
    text = colour.lstrip("#")
    return tuple(int(text[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _apply(matrix, vector):
    return tuple(sum(matrix[i][j] * vector[j] for j in range(3)) for i in range(3))


def seen_as(colour: str, vision: str) -> tuple[float, float, float]:
    """The colour as someone with *vision* sees it."""
    if vision == "normal":
        return rgb(colour)
    linear = tuple(_to_linear(c) for c in rgb(colour))
    collapsed = _apply(_COLLAPSE[vision], _apply(_RGB_LMS, linear))
    back = _apply(_LMS_RGB, collapsed)
    return tuple(min(1.0, max(0.0, _to_srgb(min(1.0, max(0.0, c))))) for c in back)  # type: ignore[return-value]


def lab(channels: tuple[float, float, float]) -> tuple[float, float, float]:
    r, g, b = (_to_linear(c) for c in channels)
    x = r * 0.4124 + g * 0.3576 + b * 0.1805
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = r * 0.0193 + g * 0.1192 + b * 0.9505

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x / 0.95047), f(y / 1.0), f(z / 1.08883)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def difference(a: str, b: str, vision: str) -> float:
    """CIE76 ``dE`` between two colours, as *vision* sees them."""
    return math.dist(lab(seen_as(a, vision)), lab(seen_as(b, vision)))


def contrast(colour: str, against: tuple[float, float, float] = WHITE) -> float:
    """The WCAG contrast ratio, for a line drawn on paper."""

    def luminance(channels):
        r, g, b = (_to_linear(c) for c in channels)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    one, two = luminance(rgb(colour)) + 0.05, luminance(against) + 0.05
    return max(one, two) / min(one, two)


def closest_pair(colours, vision: str) -> tuple[float, tuple[str, str]]:
    """The two of these that are hardest to tell apart, and by how much."""
    worst, pair = math.inf, ("", "")
    for a, b in itertools.combinations(colours, 2):
        gap = difference(a, b, vision)
        if gap < worst:
            worst, pair = gap, (a, b)
    return worst, pair
