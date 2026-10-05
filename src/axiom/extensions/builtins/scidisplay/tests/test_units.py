# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A value whose unit is not declared may not be shown as a bare number.

A live install on 2026-09-24: 23.2M of 70.1M served rows carried no unit,
and for one site 100.0% of its 19.1M did. `Power` ran to 1,170,000 and `excess_rho`
ran 0.53 to 7.81 — readings whose meaning changes completely with the unit,
served as bare numbers.
"""

from __future__ import annotations

import pytest

from ..units import (
    UNDECLARED,
    UNDECLARED_BRIEF,
    all_declared,
    annotate,
    declared,
    for_export,
    label,
    qualify,
    undeclared_count,
)


class TestWhatCountsAsDeclared:
    @pytest.mark.parametrize("unit", ["W", "degC", "psi", "%RH", "L/min", "1"])
    def test_a_stated_unit_is_declared(self, unit):
        assert declared(unit)

    @pytest.mark.parametrize("unit", [None, "", "   ", "\t", "\n"])
    def test_every_shape_of_absence_is_absent(self, unit):
        """None, empty and whitespace are told apart nowhere else in the
        pipeline, so they are not told apart here."""
        assert not declared(unit)

    def test_a_unit_is_stripped_not_rejected_for_padding(self):
        assert declared(" W ")
        assert label(" W ") == "W"


class TestNothingRendersBlank:
    def test_label_never_returns_the_empty_string(self):
        """The whole defect in one assertion: every surface did
        `unit or ""`, and the empty string is what made a number look
        dimensionless."""
        for unit in (None, "", "   "):
            assert label(unit) == UNDECLARED
            assert label(unit) != ""

    def test_the_brief_form_is_also_never_blank(self):
        assert label(None, brief=True) == UNDECLARED_BRIEF
        assert UNDECLARED_BRIEF.strip()

    def test_the_marker_is_words_not_a_question_mark(self):
        """`?` reads as "unknown value". The value is known; the unit is
        what is missing, and the reader has to be told which."""
        assert "?" not in UNDECLARED
        assert "unit" in UNDECLARED


class TestQualifyingAName:
    def test_a_declared_unit_reads_as_a_measurement(self):
        assert qualify("Power", "W") == "Power (W)"

    def test_an_absent_one_says_so_in_the_same_place(self):
        """Same position, so a reader scanning a row of headers cannot miss
        it by looking where the unit usually is."""
        assert qualify("Power", None) == "Power (unit not declared)"

    def test_the_real_channels_from_the_node(self):
        assert qualify("excess_rho", "") == "excess_rho (unit not declared)"
        assert qualify("PoolTemp", "degC") == "PoolTemp (degC)"


class TestAnnotatingALoneValue:
    def test_a_declared_unit_is_written_the_way_a_measurement_is(self):
        assert annotate("1170000", "W") == "1170000 W"

    def test_an_absent_one_is_parenthesised(self):
        """A statement ABOUT the value, not part of it — so it cannot be
        mistaken for a unit named "unit not declared"."""
        assert annotate("1170000", None) == "1170000 (unit not declared)"

    def test_it_does_not_invent_a_space_into_the_value(self):
        assert annotate("7.81", "") == "7.81 (unit not declared)"


class TestExportCells:
    def test_an_undeclared_unit_writes_the_marker_not_a_blank(self):
        """An empty cell downstream is indistinguishable from a quantity
        that needs no unit. That difference is the reason to write anything
        at all, and it survives only if something is written."""
        assert for_export(None) == UNDECLARED
        assert for_export("") == UNDECLARED
        assert for_export(None) != ""

    def test_a_declared_unit_exports_as_itself(self):
        assert for_export("degC") == "degC"


class TestSayingItOnceAtTheTop:
    def test_all_declared_is_true_only_when_every_one_is(self):
        assert all_declared(["W", "degC"])
        assert not all_declared(["W", None])
        assert not all_declared(["W", ""])

    def test_an_empty_set_has_nothing_undeclared(self):
        assert all_declared([])
        assert all_declared(None)
        assert undeclared_count(None) == 0

    def test_the_count_lets_a_surface_say_how_many(self):
        """"3 of 20 columns" is actionable; "some columns" is not."""
        assert undeclared_count(["W", None, "", "degC"]) == 2
