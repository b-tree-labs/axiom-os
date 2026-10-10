# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The simulate-train-predict pipeline, proved on realistic figures.

The shape under test recurs across every consumer of this platform: a
high-fidelity model is solved, a cheap surrogate is trained on it, the
surrogate predicts, and somebody eventually compares the prediction to what
happened. The tests below use figures from a real 161-day comparison
because a pipeline proved on round numbers proves nothing about the
arithmetic that bites.
"""

from __future__ import annotations

import math
import random

import pytest

from axiom.uncertainty import TYPE_A, Quantity, add, correlation
from axiom.uncertainty.pipeline import (
    CONDITIONING,
    CONFIGURATION,
    DOMAIN,
    INPUT,
    IRREDUCIBLE_BY_MORE_DATA,
    MODEL_FORM,
    NUMERICAL,
    SURROGATE,
    VALIDATION,
    Pipeline,
    correction_stage,
    from_parity,
    stage,
    validation_stage,
)

# A real comparison: 161 paired days, a raw offset of 79 units that a bias
# correction takes down to 40. Those two numbers are the whole reason this
# module exists, and until now they lived in a report.
RAW_BIAS = 79.0
CORRECTED_RMS = 40.0
PAIRS = 161


def _synthetic_parity(
    *, bias: float, scatter: float, n: int, seed: int = 20260928
) -> tuple[list[float], list[float]]:
    """Paired predictions and observations with a KNOWN bias and scatter.

    Generated rather than asserted so the recovery is a real measurement of
    the estimator and not a restatement of a constant.
    """
    rng = random.Random(seed)
    observed = [rng.uniform(100.0, 900.0) for _ in range(n)]
    predicted = [o + bias + rng.gauss(0.0, scatter) for o in observed]
    return predicted, observed


class TestParityRecoversWhatItShould:
    def test_bias_and_residual_are_recovered_from_paired_samples(self):
        predicted, observed = _synthetic_parity(bias=RAW_BIAS, scatter=CORRECTED_RMS, n=PAIRS)
        parity = from_parity(predicted, observed, unit="cm")

        assert parity.n == PAIRS
        # Four standard errors either side. Tight enough to catch an
        # estimator that is wrong, loose enough not to fail on the draw.
        se = CORRECTED_RMS / math.sqrt(PAIRS)
        assert abs(parity.bias - RAW_BIAS) < 4 * se
        assert abs(parity.residual_rms - CORRECTED_RMS) < 4 * CORRECTED_RMS / math.sqrt(2 * PAIRS)
        assert parity.bias_standard_error == pytest.approx(parity.residual_rms / math.sqrt(PAIRS))

    def test_the_correction_is_known_far_better_than_the_residual(self):
        """The distinction people collapse, and why it matters.

        Averaging 161 comparisons pins the OFFSET to about three units.
        It does nothing whatever to the forty units of scatter the offset
        does not explain. Treating the second as if it shrank like the first
        is how a model gets quoted an order of magnitude too confidently.
        """
        predicted, observed = _synthetic_parity(bias=RAW_BIAS, scatter=CORRECTED_RMS, n=PAIRS)
        parity = from_parity(predicted, observed, unit="cm")

        assert parity.bias_standard_error < parity.residual_rms / 10
        assert parity.residual_rms > 30.0

    def test_gain_reports_what_the_correction_buys(self):
        predicted, observed = _synthetic_parity(bias=RAW_BIAS, scatter=CORRECTED_RMS, n=PAIRS)
        parity = from_parity(predicted, observed, unit="cm")
        # hypot(79, 40) -> 88.6 raw; 40 after. About 55% removed, which is
        # the same order as the 43% RMS gain the real comparison reported.
        assert 0.4 < parity.gain < 0.7

    def test_a_correction_worth_nothing_reports_no_gain(self):
        """An unbiased model gains nothing from a bias correction, and the
        figure must say so rather than flattering the fix."""
        predicted, observed = _synthetic_parity(bias=0.0, scatter=CORRECTED_RMS, n=PAIRS)
        parity = from_parity(predicted, observed, unit="cm")
        assert parity.gain < 0.05

    def test_one_pair_establishes_nothing_and_says_so(self):
        with pytest.raises(ValueError, match="anecdote"):
            from_parity([1.0], [2.0])

    def test_unpaired_sequences_are_refused(self):
        with pytest.raises(ValueError, match="paired"):
            from_parity([1.0, 2.0, 3.0], [1.0, 2.0])

    def test_parity_reads_as_a_sentence(self):
        predicted, observed = _synthetic_parity(bias=RAW_BIAS, scatter=CORRECTED_RMS, n=PAIRS)
        text = from_parity(predicted, observed, unit="cm").reads()
        assert "161 paired comparisons" in text
        assert "does not explain" in text


class TestThePipelineComposes:
    def _full(self) -> Pipeline:
        """Every stage declared, at plausible magnitudes for this shape."""
        return (
            Pipeline(name="surrogate-prediction")
            .with_stage(
                stage(
                    CONDITIONING,
                    symbol="data_platform:resample:step_hold",
                    measurand="input aligned onto the model's time grid",
                    standard=3.0,
                    traceable_to="half the hold interval",
                )
            )
            .with_stage(
                stage(
                    MODEL_FORM,
                    symbol="solver:high-fidelity:model_form",
                    measurand="idealisation of the as-built system",
                    standard=25.0,
                    traceable_to="as-built versus as-modelled comparison",
                )
            )
            .with_stage(
                stage(
                    NUMERICAL,
                    symbol="solver:high-fidelity:discretisation",
                    measurand="discretisation of the governing equations",
                    standard=8.0,
                    traceable_to="grid convergence over three refinements",
                )
            )
            .with_stage(
                stage(
                    SURROGATE,
                    symbol="model_corral:surrogate-v3:truncation",
                    measurand="surrogate departure from its training solutions",
                    standard=12.0,
                    traceable_to="held-out reconstruction residual",
                )
            )
        )

    def test_every_stage_becomes_a_named_source(self):
        p = self._full()
        q = p.predict(640.0, unit="cm")
        assert len(q.terms) == 4
        assert q.u == pytest.approx(math.sqrt(3**2 + 25**2 + 8**2 + 12**2))

    def test_input_uncertainty_propagates_by_the_same_algebra(self):
        """The model's input is not independent of the model's output.

        A pipeline that combines them as independent understates, and the
        affine form makes getting it right the default rather than a thing
        somebody has to remember.
        """
        measured = Quantity(value=300.0, unit="cm", terms={"signals:sensor-a:cal": 5.0})
        q = self._full().predict(640.0, unit="cm", inputs=measured)

        assert "signals:sensor-a:cal" in q.terms
        # The prediction and its own input share that source, so they are
        # correlated — small, because the model adds much more than the
        # sensor, but not zero.
        assert correlation(q, measured) > 0.0

    def test_stages_of_the_same_kind_combine_within_the_kind(self):
        p = self._full().with_stage(
            stage(
                NUMERICAL,
                symbol="solver:high-fidelity:solver_tolerance",
                measurand="iterative convergence tolerance",
                standard=6.0,
            )
        )
        assert p.by_stage()[NUMERICAL] == pytest.approx(math.hypot(8.0, 6.0))

    def test_validation_stage_carries_the_observed_residual(self):
        predicted, observed = _synthetic_parity(bias=RAW_BIAS, scatter=CORRECTED_RMS, n=PAIRS)
        parity = from_parity(predicted, observed, unit="cm")
        v = validation_stage(
            parity,
            symbol="model_corral:surrogate-v3:validation",
            measurand="reduced-order prediction against reference measurement",
            valid_over="the operating range the 161 comparisons covered",
        )
        assert v.stage == VALIDATION
        assert v.budget.kind == TYPE_A
        assert v.standard == parity.residual_rms
        assert "161 paired comparisons" in v.budget.traceable_to

    def test_applying_a_measured_bias_imports_the_uncertainty_of_the_bias(self):
        """The term everyone forgets.

        Correcting by 79 does not make the correction exact. The corrected
        prediction carries the standard error of that 79, and a pipeline
        that drops it claims to know the fix perfectly.
        """
        predicted, observed = _synthetic_parity(bias=RAW_BIAS, scatter=CORRECTED_RMS, n=PAIRS)
        parity = from_parity(predicted, observed, unit="cm")
        c = correction_stage(
            parity,
            symbol="model_corral:surrogate-v3:bias_correction",
            measurand="reduced-order prediction",
        )
        assert c.stage == INPUT
        assert c.budget.kind == TYPE_A
        assert c.standard == parity.bias_standard_error
        assert c.standard > 0.0

    def test_an_unknown_stage_name_is_refused(self):
        with pytest.raises(ValueError, match="not one of"):
            stage("vibes", symbol="a:b:c", measurand="m", standard=1.0)


class TestWhereTheNextHourGoes:
    """The practically useful half. A number nobody can act on is a report;
    a number that says which stage to attack is engineering."""

    def test_dominance_is_reported_in_variance_share_not_magnitude(self):
        p = (
            Pipeline(name="p")
            .with_stage(stage(MODEL_FORM, symbol="a:b:form", measurand="m", standard=40.0))
            .with_stage(stage(NUMERICAL, symbol="a:b:disc", measurand="m", standard=20.0))
        )
        ranked = p.dominant()
        assert [r[0] for r in ranked] == [MODEL_FORM, NUMERICAL]
        # 1600 vs 400: four to one, not two to one. Halving the smaller term
        # buys 15% of the total; halving the larger buys 60%.
        assert ranked[0][3] == pytest.approx(0.8)
        assert ranked[1][3] == pytest.approx(0.2)

    def test_advice_says_refinement_pays_when_it_does(self):
        p = Pipeline(name="p").with_stage(
            stage(NUMERICAL, symbol="a:b:disc", measurand="m", standard=30.0)
        )
        assert "refinement pays" in p.advice()

    def test_advice_says_more_data_will_not_help_when_it_will_not(self):
        """The sentence that saves the wasted afternoon.

        Model-form error, a configuration mismatch and extrapolation all
        look exactly like a wide interval, and the instinct on seeing a wide
        interval is to collect more data. For these three that is work with
        a guaranteed return of zero.
        """
        for kind in IRREDUCIBLE_BY_MORE_DATA:
            p = Pipeline(name="p").with_stage(
                stage(kind, symbol="a:b:term", measurand="m", standard=50.0)
            )
            advice = p.advice()
            assert "will not reduce it" in advice, kind
            assert "different model" in advice, kind

    def test_a_pipeline_that_declared_nothing_says_nothing(self):
        p = Pipeline(name="p")
        assert p.dominant() == []
        assert "nothing can be said" in p.advice()


class TestTheRuleVandV20ExistsToEnforce:
    def test_a_prediction_cannot_be_quoted_tighter_than_its_validation(self):
        """The whole point of validating.

        Declared stages here sum to about 30 units. The observed comparison
        says 40. A pipeline reporting 30 would be claiming the model is
        better than the only measurement of how good it is — and doing so
        with a straight face, because every declared term is defensible.
        The validation stage is what makes that arithmetically impossible.
        """
        predicted, observed = _synthetic_parity(bias=RAW_BIAS, scatter=CORRECTED_RMS, n=PAIRS)
        parity = from_parity(predicted, observed, unit="cm")

        optimistic = (
            Pipeline(name="declared-only")
            .with_stage(stage(SURROGATE, symbol="m:c:trunc", measurand="m", standard=12.0))
            .with_stage(stage(NUMERICAL, symbol="s:h:disc", measurand="m", standard=8.0))
            .with_stage(stage(MODEL_FORM, symbol="s:h:form", measurand="m", standard=25.0))
        )
        honest = optimistic.with_stage(
            validation_stage(
                parity,
                symbol="m:c:validation",
                measurand="prediction against reference",
                valid_over="the compared range",
            )
        )

        declared = optimistic.predict(640.0, unit="cm").u
        with_validation = honest.predict(640.0, unit="cm").u

        # The declared account is narrower than the comparison measured.
        assert declared < parity.residual_rms
        # Reconciled to it, the prediction lands EXACTLY on the measured
        # disagreement. Not above it: adding validation in quadrature to the
        # terms it already contains would double count, and the inflated
        # figure would be just as indefensible as the optimistic one.
        assert with_validation == pytest.approx(parity.residual_rms, rel=1e-12)
        # And the shortfall in the account is carried as its own term, which
        # is the honest reading: the model is worse than its own explanation
        # of itself, by a stated amount.
        assert honest.unexplained() == pytest.approx(
            math.sqrt(parity.residual_rms**2 - declared**2)
        )
        assert honest.dominant(top=1)[0][0] == VALIDATION

    def test_extrapolation_dominates_visibly_rather_than_quietly(self):
        """Outside the envelope the answer is not "wider", it is "no".

        Carrying extrapolation as a term rather than a flag means a
        prediction outside its training range cannot be served looking
        merely imprecise — the term swamps everything and `advice()` names
        the envelope.
        """
        inside = (
            Pipeline(name="p")
            .with_stage(stage(SURROGATE, symbol="m:c:trunc", measurand="m", standard=12.0))
            .with_stage(stage(MODEL_FORM, symbol="s:h:form", measurand="m", standard=25.0))
        )
        outside = inside.with_stage(
            stage(
                DOMAIN,
                symbol="model_corral:surrogate-v3:extrapolation",
                measurand="prediction outside the validated envelope",
                standard=400.0,
                valid_over="NOT a validated bound — the surrogate was never compared here",
            )
        )
        assert outside.predict(640.0, unit="cm").u > 10 * inside.predict(640.0, unit="cm").u
        assert "envelope" in outside.advice()

    def test_a_configuration_mismatch_is_its_own_term(self):
        """Why an apparent bias may not be the model's fault.

        If the model was built for one configuration and asked about
        another, a disagreement has two explanations and the comparison
        cannot separate them. Declaring the mismatch is what stops the
        residual being attributed entirely to model error — which would
        send somebody off to fix physics that was never wrong.
        """
        p = (
            Pipeline(name="p")
            .with_stage(stage(MODEL_FORM, symbol="s:h:form", measurand="m", standard=25.0))
            .with_stage(
                stage(
                    CONFIGURATION,
                    symbol="model_corral:surrogate-v3:config_mismatch",
                    measurand="the configuration the surrogate assumes versus the actual one",
                    standard=60.0,
                    traceable_to="spread across the four configurations in the training set",
                    note=(
                        "the comparison cannot split this from model form, which is "
                        "precisely why it is declared separately"
                    ),
                )
            )
        )
        stage_name, symbol, standard, share = p.dominant(top=1)[0]
        assert stage_name == CONFIGURATION
        assert share > 0.8
        assert "will not reduce it" in p.advice()
        assert "matched configuration" in p.advice()


class TestTheSameShapeServesDifferentDomains:
    """One implementation, three pipelines, no domain in the code.

    If the abstraction is right this test is boring, and that is the
    result being asserted.
    """

    @pytest.mark.parametrize(
        ("name", "surrogate_u", "validation_u"),
        [
            ("physics-surrogate", 12.0, 40.0),
            ("process-line-twin", 0.4, 1.1),
            ("yield-forecast", 0.02, 0.08),
        ],
    )
    def test_any_pipeline_of_this_shape_composes_identically(self, name, surrogate_u, validation_u):
        p = (
            Pipeline(name=name)
            .with_stage(
                stage(SURROGATE, symbol=f"ext:{name}:trunc", measurand="m", standard=surrogate_u)
            )
            .with_stage(
                stage(
                    VALIDATION,
                    symbol=f"ext:{name}:validation",
                    measurand="m",
                    standard=validation_u,
                    evaluated=TYPE_A,
                )
            )
        )
        q = p.predict(1.0, unit="unit")
        # Each measured disagreement exceeds its declared surrogate term, so
        # in all three the prediction is reconciled up to the measurement —
        # identical arithmetic, three unrelated domains, one implementation.
        assert q.u == pytest.approx(validation_u, rel=1e-12)
        assert p.unexplained() == pytest.approx(math.sqrt(validation_u**2 - surrogate_u**2))
        assert p.dominant(top=1)[0][0] == VALIDATION

    def test_two_predictions_from_one_model_are_correlated_through_it(self):
        """The composition property that makes this worth the trouble.

        Two predictions from the same surrogate share its truncation and
        model-form sources, so a difference between them is TIGHTER than
        either — the shared error cancels. Nothing declares that; it falls
        out of the symbols, which is what "infinitely composable" buys.
        """
        p = (
            Pipeline(name="shared")
            .with_stage(stage(SURROGATE, symbol="m:c:trunc", measurand="m", standard=12.0))
            .with_stage(stage(MODEL_FORM, symbol="s:h:form", measurand="m", standard=25.0))
        )
        a = p.predict(640.0, unit="cm")
        b = p.predict(610.0, unit="cm")

        assert correlation(a, b) == pytest.approx(1.0)
        # Perfectly shared systematics: the difference of two predictions
        # from one model knows its own offset exactly.
        from axiom.uncertainty import difference

        assert difference(a, b).u == pytest.approx(0.0, abs=1e-12)
        # Whereas independent surrogates would not cancel at all.
        other = (
            Pipeline(name="independent")
            .with_stage(stage(SURROGATE, symbol="m:d:trunc", measurand="m", standard=12.0))
            .with_stage(stage(MODEL_FORM, symbol="s:i:form", measurand="m", standard=25.0))
        )
        assert difference(a, other.predict(610.0, unit="cm")).u > 38.0


class TestTheArithmeticAgreesWithTheLonghand:
    def test_pipeline_combination_equals_gum_longhand(self):
        """Independent verification, not a restatement.

        JCGM 100 §5 for uncorrelated inputs with unit sensitivity
        coefficients, computed here by hand over the declared standards.
        """
        contributors = [3.0, 25.0, 8.0, 12.0]
        kinds = [CONDITIONING, MODEL_FORM, NUMERICAL, SURROGATE]
        p = Pipeline(name="p")
        for i, (k, s) in enumerate(zip(kinds, contributors, strict=True)):
            p = p.with_stage(stage(k, symbol=f"e:s{i}:t", measurand="m", standard=s))

        longhand = math.sqrt(sum(s * s for s in contributors))
        assert p.predict(640.0, unit="cm").u == pytest.approx(longhand, rel=1e-12)

        # Reconciling to a wider measurement yields the measurement, and the
        # arithmetic is checkable by hand either way round.
        measured = 40.0
        reconciled = p.with_stage(
            stage(VALIDATION, symbol="e:v:t", measurand="m", standard=measured, evaluated=TYPE_A)
        )
        assert reconciled.unexplained() == pytest.approx(
            math.sqrt(measured**2 - longhand**2), rel=1e-12
        )
        assert reconciled.predict(640.0, unit="cm").u == pytest.approx(measured, rel=1e-12)

        # And reconciling to a NARROWER measurement changes nothing: a model
        # may not be quoted better than its own account of itself either.
        narrow = p.with_stage(
            stage(VALIDATION, symbol="e:v:t", measurand="m", standard=10.0, evaluated=TYPE_A)
        )
        assert narrow.unexplained() == 0.0
        assert narrow.predict(640.0, unit="cm").u == pytest.approx(longhand, rel=1e-12)

    def test_the_expanded_interval_is_the_coverage_factor_times_the_standard(self):
        p = Pipeline(name="p").with_stage(
            stage(VALIDATION, symbol="e:s:v", measurand="m", standard=40.0, evaluated=TYPE_A)
        )
        q = p.predict(640.0, unit="cm")
        assert q.expanded(k=2.0) == pytest.approx(80.0)

    def test_adding_predictions_from_two_pipelines_stays_closed(self):
        """A pipeline's output is an ordinary Quantity, so it composes with
        everything else in the package. There is no second algebra."""
        a = (
            Pipeline(name="a")
            .with_stage(stage(SURROGATE, symbol="m:a:t", measurand="m", standard=12.0))
            .predict(100.0, unit="cm")
        )
        b = (
            Pipeline(name="b")
            .with_stage(stage(SURROGATE, symbol="m:b:t", measurand="m", standard=5.0))
            .predict(200.0, unit="cm")
        )
        total = add([a, b])
        assert total.value == pytest.approx(300.0)
        assert set(total.terms) == {"m:a:t", "m:b:t"}
        assert math.isclose(total.low, math.hypot(12.0, 5.0), rel_tol=1e-12)
        assert total.premise == "exact"
        assert total.unquantified == 0


class TestMonteCarloAgreesWithTheStages:
    def test_sampling_the_declared_sources_reproduces_the_combined_figure(self):
        """GUM-S1 by simulation, as an independent check on the algebra.

        Each source is drawn once per trial and the prediction is formed as
        its own linear combination of those draws, so correlation is present
        by construction rather than asserted.
        """
        standards = {"e:s0:t": 3.0, "e:s1:t": 25.0, "e:s2:t": 8.0, "e:s3:t": 12.0}
        p = Pipeline(name="p")
        for i, (sym, s) in enumerate(standards.items()):
            p = p.with_stage(
                stage(
                    [CONDITIONING, MODEL_FORM, NUMERICAL, SURROGATE][i],
                    symbol=sym,
                    measurand="m",
                    standard=s,
                )
            )
        analytic = p.predict(640.0, unit="cm").u

        rng = random.Random(4242)
        draws = 40_000
        samples = []
        for _ in range(draws):
            eps = {sym: rng.gauss(0.0, 1.0) for sym in standards}
            samples.append(sum(standards[sym] * eps[sym] for sym in standards))
        mc = math.sqrt(sum(s * s for s in samples) / (draws - 1))

        # Tolerance DERIVED from the sample size, not chosen to pass: the
        # relative standard error of a standard deviation is 1/sqrt(2(n-1)),
        # and four of those is a 1-in-16000 false failure.
        tol = 4 / math.sqrt(2 * (draws - 1))
        assert mc == pytest.approx(analytic, rel=tol)


class TestTheUnexplainedResidual:
    """V&V 20's real diagnostic, and the most useful number in the module.

    Validation uncertainty MEASURES the model's total error. The other
    stages ACCOUNT for where it comes from. When the measurement exceeds
    the account, the account is incomplete — and the tempting move, which
    is to refine the largest declared term, is the wrong one.
    """

    def _with(self, *, validation: float, declared: tuple[float, ...]) -> Pipeline:
        p = Pipeline(name="p")
        for i, s in enumerate(declared):
            p = p.with_stage(stage(SURROGATE, symbol=f"e:d{i}:t", measurand="m", standard=s))
        return p.with_stage(
            stage(
                VALIDATION,
                symbol="e:v:t",
                measurand="m",
                standard=validation,
                evaluated=TYPE_A,
            )
        )

    def test_a_gap_is_reported_in_quadrature_so_it_compares_to_the_terms(self):
        # 50 measured against 30 and 40 declared: the account is exactly
        # complete, because hypot(30, 40) is 50.
        assert self._with(validation=50.0, declared=(30.0, 40.0)).unexplained() == pytest.approx(
            0.0
        )
        # 60 measured against the same account leaves sqrt(3600-2500) = 33.2.
        assert self._with(validation=60.0, declared=(30.0, 40.0)).unexplained() == pytest.approx(
            math.sqrt(3600 - 2500)
        )

    def test_an_account_that_covers_the_measurement_leaves_no_gap(self):
        assert self._with(validation=20.0, declared=(30.0, 40.0)).unexplained() == 0.0

    def test_a_pipeline_that_was_never_validated_claims_no_gap(self):
        """No comparison means nothing to compare against — not a clean bill
        of health. Silence here is the honest answer; the budget's missing
        validation row is what says the model is unproven."""
        p = Pipeline(name="p").with_stage(
            stage(SURROGATE, symbol="e:d:t", measurand="m", standard=12.0)
        )
        assert p.unexplained() == 0.0

    def test_advice_leads_with_the_missing_term_even_when_it_is_not_the_largest(self):
        """The gap is a different CLASS of finding, so rank must not bury it.

        Here the gap is 33.2 against a declared term of 40, so dominance
        ranking puts a declared term first — and acting on that ranking
        would mean refining a term that cannot possibly close the shortfall.
        """
        p = self._with(validation=60.0, declared=(30.0, 40.0))
        assert p.dominant(top=1)[0][2] == pytest.approx(40.0)  # not the gap
        advice = p.advice()
        assert "unaccounted for" in advice
        assert "nobody has written down" in advice
        assert "Find the missing term first" in advice
        assert "cannot close this" in advice
        assert "refinement pays" not in advice

    def test_advice_returns_to_dominance_when_the_account_is_complete(self):
        advice = self._with(validation=50.0, declared=(30.0, 40.0)).advice()
        assert "unaccounted for" not in advice
        assert "refinement pays" in advice

    def test_the_budget_prints_the_gap(self):
        from axiom.uncertainty.pipeline import render_budget

        text = render_budget(
            self._with(validation=60.0, declared=(30.0, 40.0)), value=100.0, unit="cm"
        )
        assert "unexplained" in text
        assert "constraint on the total, not another term" in text
        # The combined figure IS the measurement, not the measurement plus
        # the terms it already contains.
        assert "u  = 60 cm" in text

    def test_the_budget_stays_quiet_when_there_is_no_gap(self):
        from axiom.uncertainty.pipeline import render_budget

        text = render_budget(
            self._with(validation=20.0, declared=(30.0, 40.0)), value=100.0, unit="cm"
        )
        assert "unexplained" not in text
        # The account stands, because it is already wider than the comparison.
        assert "u  = 50 cm" in text

    def test_a_budget_with_nothing_declared_refuses_to_imply_exactness(self):
        from axiom.uncertainty.pipeline import render_budget

        text = render_budget(Pipeline(name="p"), value=100.0, unit="cm")
        assert "not the same as being exact" in text

    def test_terms_that_do_not_state_their_range_are_named_in_the_budget(self):
        """A budget whose terms do not say where they apply has an unknown
        range of applicability, and the report must say so rather than
        letting the reader assume it covers their case."""
        from axiom.uncertainty.pipeline import render_budget

        p = Pipeline(name="p").with_stage(
            stage(SURROGATE, symbol="e:d:t", measurand="m", standard=12.0)
        )
        text = render_budget(p, value=100.0, unit="cm")
        assert "do not state what they are valid over" in text
        assert "e:d:t" in text
