# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0
"""The colour code, as checks rather than as comments.

A palette once carried a comment claiming it was "checked for deuteranopia
separation". Measured, two of its colours sat 4.9 apart under deuteranopia,
which for two thin lines is the same colour. The comment was not written
dishonestly; it was simply never run. So every claim the colour code makes is a
test here.

Five conventions:

1. **Colour is never the only carrier.** Every series is named at the end of
   its own line, so a reader who separates none of these colours still reads
   the figure. That is what makes the numbers below a quality bar rather than
   a correctness one.
2. **Hue carries the QUANTITY.** Every temperature in a figure is a shade of
   one colour, and the reader sees they are the same kind of thing before
   reading a label. Four hues are conventions a reader already holds; the rest
   are arbitrary but stable, and the module says which is which.
3. **The same channel is the same colour.** Exactly, when it is pinned or
   declared. By hue always, whatever else is in the figure.
4. **Provenance is carried by the dash and the word, not by the hue.** A model
   wears the colour of the thing it models.
5. **When a family is too crowded to separate, the figure says so.**
"""

from __future__ import annotations

import itertools
import re
from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.scidisplay.chart_colour import (
    FAMILIES,
    assign_colours,
    contrast_on_white,
    derived_colour,
    difference,
    family_for,
    normalise_colour,
)
from axiom.extensions.builtins.scidisplay.chart_svg import Series, render_svg

from ._colour_vision import closest_pair
from ._colour_vision import difference as seen_apart

T0 = datetime(2026, 9, 22, tzinfo=UTC)

#: A line on paper. WCAG asks 3.0 of a graphical object, 4.5 of text.
CONTRAST_FLOOR = 3.0
#: Two lines of DIFFERENT quantities. Lower than a palette of unrelated colours
#: would allow, and deliberately: two of the conventions are red and green,
#: which is exactly the pair about eight percent of men cannot separate by hue.
#: Keeping the convention means the distinction is carried by LIGHTNESS, and
#: this is what that buys. Before the lightness bands were pulled apart, a
#: temperature and a pressure measured 6.9.
ACROSS_FAMILIES = 11.5
#: Two lines of the SAME quantity, which are meant to look RELATED. The
#: difference here is almost all lightness, which is the easiest difference to
#: see and the one a dichromacy leaves alone.
WITHIN_A_FAMILY = 10.0

ONE_OF_EACH = (
    ("temperature", "degC"), ("pressure", "bar"), ("flow", "L/min"),
    ("radiation", "mSv"), ("power", "W"), ("electrical", "V"),
    ("length", "mm"), ("fraction", "percent"), ("undeclared", ""),
)


def _pts(n: int, base: float = 1.0):
    return [(T0 + timedelta(minutes=i), base + i) for i in range(n)]


def _drawn(svg: str) -> list[str]:
    return re.findall(
        r'<polyline points="[^"]*" fill="none" stroke="(#[0-9a-f]{6})"', svg
    )


def _notes(svg: str) -> list[str]:
    return re.findall(r'font-style="italic">([^<]+)<', svg)


class TestHueCarriesTheQuantity:
    def test_a_unit_finds_its_family(self):
        for family, unit in ONE_OF_EACH:
            assert family_for(unit) == family

    def test_an_si_prefix_does_not_change_what_is_measured(self):
        """A prefix changes the size of a number, never the quantity."""
        for unit in ("W", "kW", "MW", "mW"):
            assert family_for(unit) == "power"
        assert derived_colour("Power", "W") == derived_colour("Power", "MW")

    def test_an_unknown_unit_is_not_guessed_at(self):
        assert family_for("bananas") == "undeclared"

    def test_temperatures_are_shades_of_one_colour(self):
        names = [("t1", "degC"), ("t2", "degC"), ("t3", "degC")]
        colours, _ = assign_colours(names)
        for a, b in itertools.combinations(colours, 2):
            assert difference(a, b) < 60.0, "same quantity should read as related"

    def test_and_a_different_quantity_does_not_look_like_them(self):
        colours, _ = assign_colours(
            [("t1", "degC"), ("p1", "W")]
        )
        assert difference(*colours) >= ACROSS_FAMILIES

    def test_the_families_that_claim_a_convention_say_so(self):
        """Four hues a reader already holds; the rest are arbitrary, and
        pretending otherwise would be inventing a convention and citing it."""
        claimed = {n for n, f in FAMILIES.items() if f.conventional}
        assert claimed == {"temperature", "pressure", "flow", "radiation"}


class TestEveryColourCarriesOnPaper:
    def test_each_family_anchor_clears_the_ratio(self):
        for _family, unit in ONE_OF_EACH:
            assert contrast_on_white(derived_colour("x", unit)) >= CONTRAST_FLOOR

    def test_and_so_does_every_shade_of_a_crowded_family(self):
        colours, _ = assign_colours([(f"T{i}", "degC") for i in range(6)])
        for colour in colours:
            assert contrast_on_white(colour) >= CONTRAST_FLOOR


class TestTheyStaySeparateUnderColourVisionDeficiency:
    def test_the_family_anchors_separate(self):
        anchors = [derived_colour("x", unit) for _f, unit in ONE_OF_EACH]
        for vision in ("normal", "deuteranopia", "protanopia"):
            gap, pair = closest_pair(anchors, vision)
            assert gap >= ACROSS_FAMILIES, f"{vision}: {pair} are {gap:.1f} apart"

    def test_shades_within_a_family_separate(self):
        for count in (2, 3, 4):
            colours, _ = assign_colours([(f"T{i}", "degC") for i in range(count)])
            for vision in ("normal", "deuteranopia", "protanopia"):
                worst = min(seen_apart(a, b, vision)
                            for a, b in itertools.combinations(colours, 2))
                assert worst >= WITHIN_A_FAMILY, f"{count} at {vision}: {worst:.1f}"


class TestTheSameChannelIsTheSameColour:
    def test_alone_it_always_takes_its_family_anchor(self):
        assert assign_colours([("Power", "W")])[0][0] == derived_colour("Power", "W")

    def test_and_keeps_it_beside_other_quantities(self):
        """Which is the promise that matters: a figure gains a temperature and
        the power line does not change colour."""
        alone, _ = assign_colours([("Power", "W")])
        crowd, _ = assign_colours(
            [("Power", "W"), ("T", "degC"), ("P", "bar"), ("F", "L/min")]
        )
        assert crowd[0] == alone[0]

    def test_the_answer_does_not_depend_on_the_order_they_were_passed(self):
        one, _ = assign_colours([("a", "degC"), ("b", "degC"), ("c", "degC")])
        two, _ = assign_colours([("c", "degC"), ("a", "degC"), ("b", "degC")])
        assert dict(zip("abc", one)) == dict(zip("cab", two))

    def test_nor_on_which_machine_drew_it(self):
        """`hash()` is randomised per process; a figure drawn twice would have
        come out in different colours."""
        assert derived_colour("Power", "W") == "#4a77c1"

    def test_a_pinned_channel_never_moves(self):
        for extra in ([], [("T1", "degC")], [("T1", "degC"), ("T2", "degC")]):
            colours, _ = assign_colours(
                [("Power", "W"), *extra], preferred={"Power": "green"}
            )
            assert colours[0] == normalise_colour("green")

    def test_declared_beats_preferred(self):
        colours, _ = assign_colours(
            [("Power", "W")], declared={"Power": "red"}, preferred={"Power": "green"}
        )
        assert colours[0] == normalise_colour("red")

    def test_a_colour_nobody_can_name_is_refused(self):
        with pytest.raises(ValueError, match="chartreuse"):
            normalise_colour("chartreuse")

    def test_a_named_colour_and_its_hex_are_the_same_answer(self):
        assert normalise_colour("blue") == normalise_colour("#0072B2")


class TestProvenanceIsNotCarriedByHue:
    def _figure(self):
        return render_svg(
            [Series("T1", _pts(3), "degC"),
             Series("T2", _pts(3, 50.0), "degC"),
             Series("T1", _pts(3, 1.0), "degC", modelled=True, against="T1")],
            title="t")

    def test_a_model_wears_the_colour_of_what_it_models(self):
        """One reserved red said "this is a model" and lost WHICH model: two
        channels and their two models drew both models identically."""
        drawn = _drawn(self._figure())
        assert drawn[0] == drawn[2] != drawn[1]

    def test_and_is_dashed(self):
        assert "stroke-dasharray" in self._figure()

    def test_and_says_so_in_words(self):
        assert "(model)" in self._figure()


class TestColourIsNeverTheOnlyCarrier:
    def test_every_series_is_named_at_its_own_line(self):
        svg = render_svg([Series("Alpha", _pts(3), "degC"),
                          Series("Beta", _pts(3, 50.0), "degC")], title="t")
        assert "Alpha" in svg and "Beta" in svg


class TestWhenAFamilyIsTooCrowded:
    def test_the_figure_says_so(self):
        many = [Series(f"T{i}", _pts(3, i * 10.0), "degC") for i in range(7)]
        assert any("tell them apart" in n for n in _notes(render_svg(many, title="t")))

    def test_and_does_not_when_it_is_not(self):
        few = [Series(f"T{i}", _pts(3, i * 10.0), "degC") for i in range(3)]
        assert not any("tell them apart" in n
                       for n in _notes(render_svg(few, title="t")))
