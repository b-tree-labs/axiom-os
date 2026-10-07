# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0
"""A channel called `corrected_cm` names its unit.

Six channels on one feed were called `corrected_cm`, `measured_cm`,
`predicted_cm`, `rom_matched_cm` and two `_interp` variants. Exactly TWO of the
six declared `cm` in the field meant for it. The other four served bare numbers
while their own names said what they were, and the surface showing them said
"no unit" six times — twice of it redundant and four times of it uselessly
vague.

Two faults in one costume, needing opposite treatments. Nothing here writes a
unit anywhere: a name is what somebody else's instrument calls a thing, and
inferring a unit from it and serving the result is the guess-as-measurement
failure the unit rule exists to stop. What a name buys is EVIDENCE — which
declaration is missing, which is wrong, and which is being said twice.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.scidisplay.unit_names import agreement, unit_in_name


class TestWhatANameCarries:
    @pytest.mark.parametrize(
        ("channel", "unit"),
        [
            ("corrected_cm", "cm"),
            ("measured_cm", "cm"),
            ("depth_mm", "mm"),
            ("fuel_temp_degc", "degC"),
            ("power_kw", "kW"),
            ("pressure_psi", "psi"),
            ("flow_lpm", "L/min"),
            ("dose_msv", "mSv"),
            ("reactivity_pcm", "pcm"),
        ],
    )
    def test_a_unit_suffix_is_read(self, channel, unit):
        got = unit_in_name(channel)
        assert got and got.unit == unit

    def test_a_qualifier_after_the_unit_does_not_hide_it(self):
        """`corrected_cm_interp` is centimetres interpolated. Four of the six
        channels that started this were `_interp` or similar, so missing them
        would have missed most of the case."""
        for channel in ("corrected_cm_interp", "measured_cm_raw", "predicted_cm_avg"):
            got = unit_in_name(channel)
            assert got and got.unit == "cm", channel

    def test_a_name_with_no_unit_carries_none(self):
        for channel in ("Power", "FuelTemp1", "flow", "rod_transient"):
            assert unit_in_name(channel) is None, channel

    def test_a_bare_unit_names_no_channel(self):
        """`cm` on its own is not a channel called centimetres."""
        assert unit_in_name("cm") is None

    def test_an_ambiguous_token_is_left_alone(self):
        """A table that guesses is worse than one that stays quiet: a wrong
        unit is a wrong number, and an absent one is at least visibly absent."""
        assert unit_in_name("valve_position") is None
        assert unit_in_name("pump_speed") is None

    @pytest.mark.parametrize("sep", ["_", "-", ".", " "])
    def test_any_ordinary_separator_works(self, sep):
        got = unit_in_name(f"corrected{sep}cm")
        assert got and got.unit == "cm"


class TestSettingTheNameAgainstTheDeclaration:
    def test_the_declaration_always_wins_the_value(self):
        """A site that says a thing knows its own instrument; a name is a label
        somebody typed. What the name changes is what a surface SAYS."""
        assert agreement("depth_mm", "m").unit == "m"

    def test_both_saying_the_same_thing_is_said_once(self):
        """`corrected_cm · cm` tells a reader nothing the name did not."""
        got = agreement("corrected_cm", "cm")
        assert got.redundant is True and got.unit == "cm"

    def test_spelling_does_not_break_the_match(self):
        assert agreement("temp_degc", "degC").redundant is True

    def test_a_name_with_no_unit_is_simply_the_declaration(self):
        got = agreement("Power", "W")
        assert got.unit == "W" and not got.redundant and not got.conflict

    def test_a_named_unit_the_declaration_omits_is_a_finding_with_a_fix(self):
        """Not "this channel has no unit" but "this channel's own name says cm
        and its declaration does not". The fix is a line in a channel map, and
        this says which line."""
        got = agreement("measured_cm", None)
        assert got.undeclared_but_named == "cm"
        assert got.unit == ""

    def test_a_disagreement_is_reported_as_worse_than_an_absence(self):
        """`depth_mm` declared in metres is a figure wrong by a thousand that
        looks fine."""
        got = agreement("depth_mm", "m")
        assert "name says mm" in got.conflict and "declaration says m" in got.conflict

    def test_neither_saying_anything_is_neither(self):
        got = agreement("FuelTemp1", "")
        assert got.unit == "" and not got.undeclared_but_named and not got.conflict

    def test_an_undeclared_marker_is_not_a_declaration(self):
        for absent in (None, "", "   "):
            assert agreement("Power", absent).unit == ""


class TestTheCaseThatStartedIt:
    """The six channels, as the node actually holds them."""

    FEED = [
        ("corrected_cm", "cm"),
        ("corrected_cm_interp", None),
        ("measured_cm", None),
        ("predicted_cm", "cm"),
        ("predicted_cm_interp", None),
        ("rom_matched_cm", None),
    ]

    def test_two_of_six_say_it_twice(self):
        said_twice = [c for c, u in self.FEED if agreement(c, u).redundant]
        assert said_twice == ["corrected_cm", "predicted_cm"]

    def test_and_the_other_four_name_a_unit_nobody_declared(self):
        named = [c for c, u in self.FEED if agreement(c, u).undeclared_but_named]
        assert len(named) == 4
        assert all(agreement(c, u).undeclared_but_named == "cm" for c, u in self.FEED
                   if agreement(c, u).undeclared_but_named)

    def test_so_the_feed_has_exactly_one_unit_and_two_kinds_of_wrong(self):
        units = {agreement(c, u).unit or agreement(c, u).undeclared_but_named
                 for c, u in self.FEED}
        assert units == {"cm"}
