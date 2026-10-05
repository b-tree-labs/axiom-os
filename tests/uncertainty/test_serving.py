# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The serving boundary: uncertainty must survive an aggregate.

The load-bearing test here is the equivalence one. A served aggregate
reconstructs the bound from sufficient statistics rather than from rows,
because a window can be millions of rows and pulling them back would
defeat the point of aggregating in the database. That shortcut is only
safe if it produces exactly what the row-wise algebra produces, so it is
proved against it over random inputs rather than asserted.
"""

from __future__ import annotations

import math
import random

import pytest

from axiom.uncertainty import MagnitudeOnly, Unquantified, add, mean
from axiom.uncertainty.serving import (
    LOOSE_PREMISE,
    combine_structured,
    companion_join_keys,
    effective_coefficient,
    for_count,
    for_dispersion,
    for_extremum,
    for_mean,
    for_sum,
    uncertainty_column_for,
)


def _stats(magnitudes: list[float]) -> dict:
    """Exactly what one SQL pass can produce over an uncertainty column."""
    return {
        "quantified": len(magnitudes),
        "sum_u": sum(magnitudes),
        "sum_sq": sum(u * u for u in magnitudes),
        "max_u": max(magnitudes, default=0.0),
    }


class TestTheShortcutEqualsTheRowWiseAlgebra:
    """The whole basis for aggregating in the database."""

    @pytest.mark.parametrize("seed", range(25))
    def test_sum_from_statistics_equals_sum_from_rows(self, seed):
        rng = random.Random(seed)
        n = rng.randint(1, 40)
        magnitudes = [rng.uniform(0.001, 10.0) for _ in range(n)]

        by_rows = add([MagnitudeOnly(value=0.0, unit="degC", u=u) for u in magnitudes])
        by_stats = for_sum(**_stats(magnitudes))

        assert by_stats.low == pytest.approx(by_rows.low, rel=1e-12)
        assert by_stats.high == pytest.approx(by_rows.high, rel=1e-12)
        assert by_stats.low_unconstrained == pytest.approx(
            by_rows.low_unconstrained, rel=1e-12, abs=1e-12
        )

    @pytest.mark.parametrize("seed", range(25))
    def test_mean_from_statistics_equals_mean_from_rows(self, seed):
        rng = random.Random(1000 + seed)
        n = rng.randint(1, 40)
        magnitudes = [rng.uniform(0.001, 10.0) for _ in range(n)]

        by_rows = mean([MagnitudeOnly(value=0.0, unit="degC", u=u) for u in magnitudes])
        by_stats = for_mean(**_stats(magnitudes))

        assert by_stats.low == pytest.approx(by_rows.low, rel=1e-12)
        assert by_stats.high == pytest.approx(by_rows.high, rel=1e-12)
        assert by_stats.low_unconstrained == pytest.approx(
            by_rows.low_unconstrained, rel=1e-12, abs=1e-12
        )

    @pytest.mark.parametrize("seed", range(15))
    def test_the_unquantified_count_carries_the_same_way(self, seed):
        rng = random.Random(2000 + seed)
        magnitudes = [rng.uniform(0.001, 10.0) for _ in range(rng.randint(1, 20))]
        blind = rng.randint(1, 10)

        by_rows = mean(
            [MagnitudeOnly(value=0.0, unit="degC", u=u) for u in magnitudes]
            + [Unquantified(value=0.0, unit="degC") for _ in range(blind)]
        )
        by_stats = for_mean(**_stats(magnitudes), unquantified=blind)

        assert by_stats.unquantified == by_rows.unquantified == blind
        # EXACT equality, not a fudge factor. The row-wise `mean` divides by
        # len(items) including the unquantified ones, and the reconstruction
        # must divide by the same thing or it is answering about a different
        # quantity than the value it accompanies.
        #
        # This assertion previously carried a `* counted / len(magnitudes)`
        # correction, which is how a test comes to accommodate a bug instead
        # of catching it: the factor was exactly the discrepancy.
        assert by_stats.low == pytest.approx(by_rows.low, rel=1e-12)
        assert by_stats.high == pytest.approx(by_rows.high, rel=1e-12)


class TestAMeanOverOneSensorCannotAverageAwayItsCalibration:
    """The defect this boundary existed to produce, at the served surface.

    A thousand readings from one sensor, each ±0.5 because of a shared
    calibration. The independent answer is 0.016. The truth is 0.5. Serving
    the first is the overconfidence that every naive implementation reports,
    because independence is what root-sum-square gives.
    """

    def test_the_bound_spans_independent_to_perfectly_shared(self):
        n = 1000
        served = for_mean(**_stats([0.5] * n))

        assert served.low == pytest.approx(0.5 / math.sqrt(n), rel=1e-12)
        assert served.high == pytest.approx(0.5, rel=1e-12)
        # The shared systematic sits at the TOP of that range, and the range
        # is what makes serving the bottom of it a stated assumption rather
        # than a silent one.
        assert served.premise == LOOSE_PREMISE
        assert not served.exact

    def test_the_sentence_says_which_end_rests_on_what(self):
        text = for_mean(**_stats([0.5] * 1000)).reads(unit="degC")
        assert "between 0.0158114 and 0.5 degC" in text
        assert "how they correlate is not" in text
        assert LOOSE_PREMISE in text

    def test_one_reading_has_nothing_to_correlate_with_and_is_exact(self):
        served = for_mean(**_stats([0.5]))
        assert served.exact
        assert served.premise == "exact"
        assert served.low == served.high == pytest.approx(0.5)


class TestAbsenceIsTransmittedNotOmitted:
    def test_no_row_reporting_an_uncertainty_claims_none(self):
        served = for_mean(quantified=0, sum_u=0.0, sum_sq=0.0, max_u=0.0, unquantified=42)
        assert not served.claimable
        assert served.low is None and served.high is None
        assert served.unquantified == 42
        assert "none is claimed" in served.reads()
        # Not zero. Zero would be a claim of perfect precision over 42 rows
        # that said nothing at all.
        assert served.payload()["low"] is None

    def test_a_partial_window_says_what_the_range_does_not_cover(self):
        served = for_mean(**_stats([0.5, 0.5, 0.5]), unquantified=7)
        assert served.claimable
        assert not served.complete
        text = served.reads(unit="degC")
        assert "covers 3 of 10 rows" in text
        assert "outside it" in text

    def test_a_complete_window_says_nothing_extra(self):
        served = for_mean(**_stats([0.5, 0.5, 0.5]))
        assert served.complete
        assert "outside it" not in served.reads()

    def test_the_payload_always_carries_every_field(self):
        """A reader must never have to infer absence from a missing key."""
        for served in (
            for_mean(**_stats([0.5])),
            for_mean(quantified=0, sum_u=0.0, sum_sq=0.0, max_u=0.0, unquantified=3),
        ):
            p = served.payload()
            assert set(p) == {
                "low",
                "high",
                "low_unconstrained",
                "premise",
                "quantified",
                "unquantified",
                "complete",
                "claimable",
                "note",
                "structured",
                "terms",
                "dominant",
            }


class TestAnExtremumIsARowNotACombination:
    def test_the_selected_rows_own_uncertainty_is_reported_exactly(self):
        served = for_extremum(fn="max", selected_u=0.5, rivals=0, quantified=100)
        assert served.exact
        assert served.low == served.high == pytest.approx(0.5)
        assert served.premise == "exact"

    def test_rivals_within_the_interval_make_the_extremum_undetermined(self):
        """The finding nothing reported before, and the number looks decisive.

        A served peak of 91.2 ± 0.5 with forty other samples inside that
        interval is not the location of a peak. Anything downstream treating
        it as one — an alarm threshold, a limit check, a headline — is acting
        on noise.
        """
        served = for_extremum(fn="max", selected_u=0.5, rivals=40, quantified=100)
        assert "40 other row(s) fall within that interval" in served.note
        assert "not determined by the data" in served.note
        assert "not determined by the data" in served.reads(unit="degC")

    def test_an_unambiguous_extremum_says_nothing_extra(self):
        served = for_extremum(fn="min", selected_u=0.5, rivals=0, quantified=100)
        assert served.note == ""

    def test_an_extremum_row_with_no_uncertainty_claims_none(self):
        served = for_extremum(fn="max", selected_u=None, rivals=0, quantified=0, unquantified=5)
        assert not served.claimable
        assert "reported no uncertainty" in served.reads()


class TestDispersionIsInflatedNotAdded:
    def test_measurement_magnitude_is_reported_as_a_floor_not_a_term(self):
        served = for_dispersion(observed=5.0, mean_u=0.5, quantified=100)
        # Deliberately not claimable: a dispersion's own uncertainty is a
        # different quantity from the measurement magnitude.
        assert not served.claimable
        assert "includes it rather than adding to it" in served.note

    def test_a_spread_that_may_be_entirely_instrumental_says_so(self):
        """The most actionable thing this boundary can say about a std.

        If the observed dispersion does not exceed the mean measurement
        magnitude, nothing in the data establishes that the process varied
        at all.
        """
        served = for_dispersion(observed=0.4, mean_u=0.5, quantified=100)
        assert "may be entirely instrumental" in served.note
        assert "reading the instrument" in served.note

    def test_a_dispersion_with_no_magnitudes_says_the_split_is_unknown(self):
        served = for_dispersion(observed=5.0, mean_u=None, quantified=0, unquantified=100)
        assert not served.claimable
        assert "is unknown" in served.note


class TestACountIsExactlyKnown:
    def test_zero_here_is_a_true_zero(self):
        served = for_count(quantified=0, unquantified=10)
        assert served.claimable
        assert served.low == served.high == 0.0
        assert "exactly known" in served.note


class TestFindingTheUncertaintyColumn:
    def test_value_pairs_with_the_plain_uncertainty_column(self):
        assert uncertainty_column_for("value", {"value", "uncertainty", "ts"}) == "uncertainty"

    def test_any_other_column_pairs_with_an_explicit_sibling(self):
        assert uncertainty_column_for("flow", {"flow", "flow_uncertainty"}) == "flow_uncertainty"

    def test_a_column_with_no_sibling_gets_nothing(self):
        assert uncertainty_column_for("flow", {"flow", "uncertainty"}) is None

    def test_nothing_is_inferred_from_a_column_merely_being_numeric(self):
        """A float column is not an error bar. Guessing would attach an
        unrelated measurement to a value as though it bounded it."""
        assert uncertainty_column_for("temperature", {"temperature", "pressure"}) is None


class TestTheCombinationMathematics:
    def test_high_is_unconditional(self):
        """The triangle inequality in L². Nothing is assumed."""
        magnitudes = [0.5, 0.3, 0.9, 0.1]
        assert for_sum(**_stats(magnitudes)).high == pytest.approx(sum(magnitudes))

    def test_low_is_root_sum_square_under_the_stated_premise(self):
        magnitudes = [0.5, 0.3, 0.9, 0.1]
        assert for_sum(**_stats(magnitudes)).low == pytest.approx(
            math.sqrt(sum(u * u for u in magnitudes))
        )

    def test_the_unconstrained_floor_allows_full_cancellation(self):
        """Two equal magnitudes at r = -1 cancel to zero, so the complete
        floor must be zero there — and it is usually zero, which is why it
        is not the headline."""
        assert for_sum(**_stats([0.5, 0.5])).low_unconstrained == pytest.approx(0.0)
        # One magnitude dominating cannot be cancelled below its excess.
        assert for_sum(**_stats([10.0, 1.0, 1.0])).low_unconstrained == pytest.approx(8.0)

    def test_scaling_a_sum_into_a_mean_scales_every_endpoint(self):
        magnitudes = [10.0, 1.0, 1.0]
        s = for_sum(**_stats(magnitudes))
        m = for_mean(**_stats(magnitudes))
        for got, expected in (
            (m.low, s.low / 3),
            (m.high, s.high / 3),
            (m.low_unconstrained, s.low_unconstrained / 3),
        ):
            assert got == pytest.approx(expected, rel=1e-12)

    def test_the_bound_brackets_the_root_sum_square_answer(self):
        """Whatever the correlation, the honest range contains the answer a
        naive independent implementation would have served — which is what
        makes the range an improvement rather than a different guess."""
        rng = random.Random(7)
        for _ in range(50):
            magnitudes = [rng.uniform(0.01, 5.0) for _ in range(rng.randint(2, 30))]
            served = for_sum(**_stats(magnitudes))
            naive = math.sqrt(sum(u * u for u in magnitudes))
            assert served.low_unconstrained <= naive <= served.high + 1e-12
            assert served.low == pytest.approx(naive)


# ---------------------------------------------------------------------------
# The companion table: structure, and what it buys
# ---------------------------------------------------------------------------


class TestSharedVersusPerReadingIsTheWholeGame:
    """`independent` is the one field that decides whether error averages away.

    Getting it backwards is the most consequential modelling error available
    at this boundary, which is why the column defaults to shared: a wrong
    `false` is a bound that is too wide, a wrong `true` is a number that is
    confidently incorrect.
    """

    def test_a_shared_source_does_not_average_away(self):
        """1000 readings, one calibration bath, ±0.5 each.

        The coefficients SUM to 500, and a mean divides by 1000 — giving back
        exactly 0.5. Averaging a thousand readings does not average away the
        calibration, and the arithmetic says so without being told.
        """
        got = effective_coefficient(
            sum_a=0.5 * 1000, sum_sq=0.25 * 1000, independent=False, scale=1 / 1000
        )
        assert got == pytest.approx(0.5)

    def test_a_per_reading_source_averages_down_like_one_over_root_n(self):
        """The same magnitude declared as per-reading noise instead."""
        got = effective_coefficient(
            sum_a=0.1 * 1000, sum_sq=0.01 * 1000, independent=True, scale=1 / 1000
        )
        assert got == pytest.approx(0.1 / math.sqrt(1000))

    def test_only_the_independent_kind_earns_the_averaging(self):
        n = 400
        shared = effective_coefficient(
            sum_a=0.5 * n, sum_sq=0.25 * n, independent=False, scale=1 / n
        )
        per_reading = effective_coefficient(
            sum_a=0.5 * n, sum_sq=0.25 * n, independent=True, scale=1 / n
        )
        assert shared == pytest.approx(0.5)
        assert per_reading == pytest.approx(0.5 / 20)
        assert shared / per_reading == pytest.approx(math.sqrt(n))

    def test_a_sum_does_not_scale_either_kind(self):
        for independent in (False, True):
            got = effective_coefficient(sum_a=6.0, sum_sq=12.0, independent=independent, scale=1.0)
            assert got == pytest.approx(6.0 if not independent else math.sqrt(12.0))


class TestDeclaringStructureNarrowsTheAnswer:
    """The incentive has to point the right way.

    If declaring provenance made the served answer wider, nobody would ever
    declare it. It makes it narrower, because correlation stops being unknown.
    """

    def test_the_same_magnitudes_are_exact_once_their_sources_are_named(self):
        # Undeclared: 1000 readings at ±0.5, correlation unknown -> a bound.
        bounded = for_mean(**_stats([0.5] * 1000))
        assert bounded.low == pytest.approx(0.5 / math.sqrt(1000))
        assert bounded.high == pytest.approx(0.5)
        assert not bounded.exact

        # Declared: one shared calibration source. Exactly 0.5, no range.
        declared = combine_structured(
            terms={"signals:cal-bath-a:offset": 0.5}, structured_rows=1000, fn="mean"
        )
        assert declared.exact
        assert declared.low == declared.high == pytest.approx(0.5)
        assert declared.premise == "exact"

        # And the exact answer sits INSIDE the bound the magnitudes alone gave,
        # which is what makes the bound a bound rather than a different guess.
        assert bounded.low <= declared.low <= bounded.high

    def test_two_sources_compose_in_quadrature_on_their_symbols(self):
        served = combine_structured(
            terms={"signals:cal-bath-a:offset": 0.5, "signals:tc-14:repeatability": 0.1},
            structured_rows=100,
            fn="mean",
        )
        assert served.exact
        assert served.low == pytest.approx(math.hypot(0.5, 0.1))

    def test_the_dominant_source_is_named_so_effort_can_be_aimed(self):
        served = combine_structured(
            terms={"signals:cal-bath-a:offset": 0.5, "signals:tc-14:repeatability": 0.1},
            structured_rows=100,
            fn="mean",
        )
        top = served.dominant()
        assert top[0]["symbol"] == "signals:cal-bath-a:offset"
        # Variance share, not magnitude: 0.25 of 0.26 is 96%, so recalibrating
        # is worth 24 times what quieting the sensor is.
        assert top[0]["share"] == pytest.approx(0.25 / 0.26)
        assert served.structured is True


class TestMixingStructuredAndUnstructuredRows:
    """ADR-136 D5's three kinds, at the served boundary.

    A row that declared structure must not ALSO contribute its scalar — the
    scalar summarises the same sources, so counting both double counts.
    """

    def test_structure_and_magnitudes_give_a_bound_around_the_structured_part(self):
        served = combine_structured(
            terms={"signals:cal-bath-a:offset": 0.5},
            structured_rows=100,
            loose_quantified=4,
            loose_sum_u=0.8,
            loose_sum_sq=0.16,
            loose_max_u=0.2,
            fn="sum",
        )
        assert not served.exact
        assert served.low == pytest.approx(math.sqrt(0.5**2 + 0.16))
        assert served.high == pytest.approx(0.5 + 0.8)
        assert served.premise == LOOSE_PREMISE
        assert "without its sources" in served.note

    def test_rows_reporting_nothing_stay_outside_the_bound_and_are_counted(self):
        served = combine_structured(
            terms={"signals:cal-bath-a:offset": 0.5},
            structured_rows=90,
            unquantified=10,
            fn="mean",
        )
        assert served.claimable
        assert not served.complete
        assert served.unquantified == 10
        # The bound is exact over what declared, and says what it does not cover.
        assert served.low == served.high == pytest.approx(0.5)
        assert "outside it" in served.reads(unit="degC")

    def test_a_window_with_no_structure_and_no_magnitudes_claims_nothing(self):
        served = combine_structured(terms={}, unquantified=50, fn="mean")
        assert not served.claimable
        assert "none is claimed" in served.note

    def test_all_three_kinds_at_once(self):
        served = combine_structured(
            terms={"signals:cal-bath-a:offset": 0.5},
            structured_rows=70,
            loose_quantified=20,
            loose_sum_u=2.0,
            loose_sum_sq=0.2,
            loose_max_u=0.1,
            unquantified=10,
            fn="sum",
        )
        assert served.quantified == 90
        assert served.unquantified == 10
        assert served.low < served.high
        assert served.structured


class TestDiscoveringACompanionTable:
    """Convention, not configuration: no table name is hardcoded anywhere."""

    def test_join_keys_are_whatever_is_not_a_term_column(self):
        assert companion_join_keys(
            {"row_hash", "channel", "symbol", "coefficient", "independent"}
        ) == ("channel", "row_hash")

    def test_a_table_without_the_term_columns_is_not_a_companion(self):
        assert companion_join_keys({"row_hash", "channel", "value"}) is None
        # symbol alone is not enough; a companion needs the coefficient too.
        assert companion_join_keys({"row_hash", "symbol"}) is None

    def test_a_companion_with_no_join_keys_is_refused(self):
        """Nothing to attach the terms to."""
        assert companion_join_keys({"symbol", "coefficient", "independent"}) is None

    def test_a_new_conformed_shape_gets_a_companion_by_naming_alone(self):
        assert companion_join_keys({"reading_id", "symbol", "coefficient", "independent"}) == (
            "reading_id",
        )


class TestSamplingProvesTheIndependentFlag:
    """The load-bearing field, checked by simulation rather than by algebra.

    `independent` decides whether an error averages away, and every other
    test here checks it against the formula it was derived from. This one
    builds the two physical situations, draws them, and measures the spread —
    so a sign error or a swapped branch shows up as a number that does not
    match reality rather than as a formula that agrees with itself.

    numpy is already a dependency and was not being used for this; a Python
    loop could not afford enough draws to make the tolerance tight.
    """

    DRAWS = 200_000
    N = 400

    @property
    def tol(self) -> float:
        """Derived from the sample size, never chosen to pass.

        The relative standard error of a sample standard deviation is
        1/sqrt(2(n-1)); five of those is a false-failure rate under 1e-6.
        """
        return 5 / math.sqrt(2 * (self.DRAWS - 1))

    def test_a_shared_source_measured_by_drawing_it_once_per_trial(self):
        """One bath, 400 readings. The bath is ONE draw the whole window sees.

        The measured spread of the mean is the bath's own magnitude, not
        anything smaller. Averaging cannot reduce an error every reading
        shares, and that is what the arithmetic has to reproduce.
        """
        np = pytest.importorskip("numpy")
        rng = np.random.default_rng(20260928)

        a = 0.5
        # One draw per trial, applied to all N readings -> the mean of N
        # identical perturbations is the perturbation.
        shared = rng.normal(0.0, a, size=self.DRAWS)
        measured = shared.std(ddof=1)

        predicted = effective_coefficient(
            sum_a=a * self.N, sum_sq=a * a * self.N, independent=False, scale=1 / self.N
        )
        assert predicted == pytest.approx(a)
        assert measured == pytest.approx(predicted, rel=self.tol)

    def test_a_per_reading_source_measured_by_drawing_it_n_times_per_trial(self):
        """Fresh noise per reading. The mean's spread falls as 1/sqrt(N)."""
        np = pytest.importorskip("numpy")
        rng = np.random.default_rng(4242)

        a = 0.5
        draws = 40_000  # N independent draws per trial, so fewer trials
        per_reading = rng.normal(0.0, a, size=(draws, self.N)).mean(axis=1)
        measured = per_reading.std(ddof=1)

        predicted = effective_coefficient(
            sum_a=a * self.N, sum_sq=a * a * self.N, independent=True, scale=1 / self.N
        )
        assert predicted == pytest.approx(a / math.sqrt(self.N))
        tol = 5 / math.sqrt(2 * (draws - 1))
        assert measured == pytest.approx(predicted, rel=tol)

    def test_the_two_kinds_together_reproduce_the_composed_figure(self):
        """A shared bath plus per-reading noise, drawn as the physics works.

        This is the case the companion table exists for, and the one a scalar
        column cannot represent: the composed figure has to match a
        simulation in which the bath is one draw and the noise is N.
        """
        np = pytest.importorskip("numpy")
        rng = np.random.default_rng(7)

        bath, rep = 0.5, 0.12
        draws = 40_000
        shared = rng.normal(0.0, bath, size=(draws, 1))
        noise = rng.normal(0.0, rep, size=(draws, self.N))
        measured = (shared + noise).mean(axis=1).std(ddof=1)

        served = combine_structured(
            terms={
                "signals:cal-bath-a:offset": effective_coefficient(
                    sum_a=bath * self.N,
                    sum_sq=bath * bath * self.N,
                    independent=False,
                    scale=1 / self.N,
                ),
                "signals:tc-14:repeatability": effective_coefficient(
                    sum_a=rep * self.N,
                    sum_sq=rep * rep * self.N,
                    independent=True,
                    scale=1 / self.N,
                ),
            },
            structured_rows=self.N,
            fn="mean",
        )
        assert served.exact
        tol = 5 / math.sqrt(2 * (draws - 1))
        assert served.low == pytest.approx(measured, rel=tol)

    def test_getting_the_flag_backwards_is_measurably_wrong(self):
        """Why the column defaults to shared.

        Declaring a shared bath as per-reading claims it averages away. At
        N=400 that is a figure 20 times too confident — and it looks
        perfectly reasonable on a chart, which is the danger.
        """
        np = pytest.importorskip("numpy")
        rng = np.random.default_rng(11)

        a = 0.5
        truth = rng.normal(0.0, a, size=self.DRAWS).std(ddof=1)
        wrong = effective_coefficient(
            sum_a=a * self.N, sum_sq=a * a * self.N, independent=True, scale=1 / self.N
        )
        assert wrong < truth / 15
        assert truth / wrong == pytest.approx(math.sqrt(self.N), rel=self.tol)
