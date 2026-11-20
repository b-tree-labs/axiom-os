# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The VALIDATE stage: what the store should hold about a judged frame.

ADR-132's taxonomy has never fired — `quality` reads `good` on all 12,967
fuel-temperature readings of exactly 0 degC — because there was no stage with
the authority to say otherwise. These are the rules that stage follows.
"""

from __future__ import annotations

import math

import pytest

from axiom.extensions.builtins.data_platform import company, validate

WARM_FUEL_ZERO = company.ZeroWhileCompanionAbove(
    zero=["FuelTemp1", "FuelTemp2"],
    companion=["WaterTemp"],
    above=5.0,
    subject_reason="company.zero_while_companion_warm",
)
OVER_TWIN = company.ExceedsTwinBy(
    subject="Power",
    twin="DT_model_power",
    factor=100.0,
    subject_reason="company.measured_exceeds_model_twin",
)


def frame(**kw):
    return {k: company.Reading(k, v) for k, v in kw.items()}


class TestBadNullsTheValueAndKeepsTheRaw:
    def test_a_zeroed_thermocouple_is_nulled(self):
        out = {
            v.channel: v
            for v in validate.run(
                frame(FuelTemp1=0.0, FuelTemp2=0.0, WaterTemp=20.0), [WARM_FUEL_ZERO]
            )
        }
        assert out["FuelTemp1"].value is None
        assert out["FuelTemp1"].quality == "bad"

    def test_the_raw_reading_survives_so_the_decision_is_reversible(self):
        """A later reader must be able to see what the instrument actually said
        and disagree with us."""
        out = {
            v.channel: v
            for v in validate.run(
                frame(FuelTemp1=0.0, FuelTemp2=0.0, WaterTemp=20.0), [WARM_FUEL_ZERO]
            )
        }
        assert out["FuelTemp1"].raw_value == 0.0

    def test_bad_with_a_value_still_set_is_refused(self):
        """The invariant, enforced at construction rather than trusted. A
        fabricated reading left in place is averaged, maxed and charted as a
        real one — NULL is what SQL excludes from `avg` by itself."""
        with pytest.raises(ValueError, match="null the value"):
            validate.Validated("FuelTemp1", 0.0, 0.0, "bad")

    def test_the_implicated_companion_keeps_its_value(self):
        out = {
            v.channel: v
            for v in validate.run(
                frame(FuelTemp1=0.0, FuelTemp2=0.0, WaterTemp=20.0), [WARM_FUEL_ZERO]
            )
        }
        assert out["WaterTemp"].quality == "suspect"
        assert out["WaterTemp"].value == 20.0


class TestValidationIsAConstraintNotATerm:
    """ADR-136's first decision, which took a correction to get right."""

    def test_a_complete_account_leaves_nothing_unexplained(self):
        """Declared terms already covering the observed disagreement means
        validation adds NOTHING — a value may not be quoted worse than its own
        account either."""
        assert validate.unexplained(observed=3.0, declared=[3.0]) == 0.0
        assert validate.unexplained(observed=3.0, declared=[5.0]) == 0.0

    def test_only_the_part_the_budget_does_not_explain_is_added(self):
        """sqrt(5**2 - 3**2) = 4. Not 5, which would double count the declared
        term the measurement already contains; and not 8, which would add them."""
        assert validate.unexplained(observed=5.0, declared=[3.0]) == pytest.approx(4.0)

    def test_declared_terms_combine_in_quadrature(self):
        assert validate.unexplained(observed=13.0, declared=[3.0, 4.0]) == pytest.approx(12.0)

    def test_a_shortfall_is_never_negative(self):
        assert validate.unexplained(observed=1.0, declared=[100.0]) == 0.0

    def test_a_positive_gap_is_the_diagnostic(self):
        """The V&V point: a gap means a real error source is MISSING from the
        budget, and refining the largest declared term cannot close it."""
        gap = validate.unexplained(observed=1_080_000.0, declared=[0.02 * 1_080_000.0])
        assert gap > 1_000_000.0


class TestItNeverNarrowsAndNeverReportsZeroForUnknown:
    def test_a_negative_widening_is_refused(self):
        with pytest.raises(ValueError, match="NARROW"):
            validate.Validated("c", 1.0, 1.0, "suspect", widen_by=-0.5)

    def test_nothing_declared_reports_absence_not_zero(self):
        """The common real state. Zero would be a claim of perfect precision
        about a reading we have just called suspect."""
        out = {
            v.channel: v
            for v in validate.run(frame(Power=1_080_000.0, DT_model_power=1.2e-4), [OVER_TWIN])
        }
        assert out["Power"].absence == validate.NOTHING_REPORTED
        assert out["Power"].widen_by == 0.0

    def test_a_declared_budget_plus_a_gap_reports_magnitude_only(self):
        """The disagreement bounds the shortfall but says nothing about what it
        correlates with, which is exactly MagnitudeOnly's meaning."""
        declared = validate.Declared({"Power": [0.02 * 1_080_000.0]})
        out = {
            v.channel: v
            for v in validate.run(
                frame(Power=1_080_000.0, DT_model_power=1.2e-4), [OVER_TWIN], declared=declared
            )
        }
        assert out["Power"].absence == validate.MAGNITUDE_ONLY
        assert out["Power"].widen_by > 0

    def test_the_absence_kind_is_always_one_of_the_three(self):
        with pytest.raises(ValueError, match="one of"):
            validate.Validated("c", 1.0, 1.0, "suspect", absence="probably-fine")

    def test_only_adr132s_closed_vocabulary_is_accepted(self):
        with pytest.raises(ValueError, match="closed vocabulary"):
            validate.Validated("c", 1.0, 1.0, "dodgy")


class TestACategoricalContradictionOffersNoMagnitude:
    def test_a_zero_rule_computes_no_shortfall_even_with_a_budget(self):
        """ "This reads exactly 0 while that one is warm" has no magnitude.
        Inventing one so the arithmetic works would put a made-up figure into an
        uncertainty account."""
        declared = validate.Declared({"WaterTemp": [0.5]})
        out = {
            v.channel: v
            for v in validate.run(
                frame(FuelTemp1=0.0, FuelTemp2=0.0, WaterTemp=20.0),
                [WARM_FUEL_ZERO],
                declared=declared,
            )
        }
        assert out["WaterTemp"].widen_by == 0.0
        assert out["WaterTemp"].absence == validate.SOURCES_KNOWN

    def test_the_comparison_rule_does_carry_one(self):
        v = company.judge(frame(Power=1_080_000.0, DT_model_power=1.2e-4), [OVER_TWIN])[0]
        assert v.observed == pytest.approx(1_080_000.0 - 1.2e-4)


class TestSilenceAboutAChannelIsTheRightOutput:
    def test_a_clean_frame_records_nothing(self):
        """Emitting a `good` row for every channel would bury the findings in
        the answer."""
        assert (
            validate.run(frame(FuelTemp1=305.0, FuelTemp2=366.0, WaterTemp=23.0), [WARM_FUEL_ZERO])
            == []
        )

    def test_a_channel_no_rule_mentions_is_absent_from_the_result(self):
        out = {
            v.channel
            for v in validate.run(
                frame(FuelTemp1=0.0, FuelTemp2=0.0, WaterTemp=20.0, LinPower=5.0), [WARM_FUEL_ZERO]
            )
        }
        assert "LinPower" not in out


class TestTheShortfallSymbolIsSharedNotPerRow:
    def test_one_symbol_for_the_whole_stage(self):
        """A shortfall found by comparing two channels is not independent of one
        found the same way on the next row. A symbol per row would let a
        thousand of them average away — understating by sqrt(n), which is the
        wrong direction and looks entirely reasonable on a chart."""
        assert validate.SHORTFALL_SYMBOL.count(":") == 2
        assert validate.SHORTFALL_SYMBOL.startswith("data_platform:")

    def test_the_gap_uses_fsum_so_many_small_terms_do_not_drift(self):
        many = [1e-8] * 10_000
        got = validate.unexplained(observed=1.0, declared=many)
        assert got == pytest.approx(math.sqrt(1.0 - 10_000 * 1e-16))
