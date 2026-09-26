# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""McNemar's test: the right instrument for the comparison we are actually running.

The arms are PAIRED — the same items under two conditions, with binary outcomes.
That is exactly what McNemar's test is for, and the spread heuristic shipped
first was a weaker stand-in: it compares a delta against run-to-run variance and
never looks at which items changed, so it cannot distinguish "both arms got a
different random half right" from "the candidate fixed the items the baseline
failed".

Only DISCORDANT pairs carry information. Items both arms got right, or both got
wrong, tell us nothing about which arm is better — discarding them is the test,
not a shortcut.
"""

from __future__ import annotations

from axiom.evals.significance import mcnemar


def _outcomes(pattern: str) -> list[bool]:
    return [c == "1" for c in pattern]


def test_identical_arms_are_never_significant():
    """The negative control, again, at the level of the test itself."""
    same = _outcomes("10110010")
    result = mcnemar(baseline=same, candidate=same)
    assert result.discordant_baseline_only == 0
    assert result.discordant_candidate_only == 0
    assert result.p_value == 1.0
    assert result.significant is False


def test_a_clean_improvement_on_enough_items_is_significant():
    """Candidate fixes twelve items the baseline failed and breaks none."""
    baseline = _outcomes("0" * 12 + "1" * 8)
    candidate = _outcomes("1" * 12 + "1" * 8)
    result = mcnemar(baseline=baseline, candidate=candidate)
    assert result.discordant_candidate_only == 12
    assert result.discordant_baseline_only == 0
    assert result.p_value < 0.05
    assert result.significant is True


def test_a_regression_is_significant_and_signed():
    """The test must be able to say the candidate is worse, not just 'differs'."""
    baseline = _outcomes("1" * 12 + "1" * 8)
    candidate = _outcomes("0" * 12 + "1" * 8)
    result = mcnemar(baseline=baseline, candidate=candidate)
    assert result.discordant_baseline_only == 12
    assert result.p_value < 0.05
    assert result.favors == "baseline"


def test_a_small_lead_on_few_items_is_not_significant():
    """Two items changed in the candidate's favour out of twenty. A delta of
    +0.10 looks like progress and is not evidence — this is the case the spread
    heuristic got wrong most often."""
    baseline = _outcomes("00" + "1" * 18)
    candidate = _outcomes("11" + "1" * 18)
    result = mcnemar(baseline=baseline, candidate=candidate)
    assert result.discordant_candidate_only == 2
    assert result.p_value > 0.05
    assert result.significant is False


def test_concordant_items_are_discarded_not_counted():
    """Adding items both arms get right must not manufacture significance. An
    n-based test would drift toward 'significant' simply by adding easy items."""
    small = mcnemar(baseline=_outcomes("00" + "1" * 8), candidate=_outcomes("11" + "1" * 8))
    padded = mcnemar(baseline=_outcomes("00" + "1" * 998), candidate=_outcomes("11" + "1" * 998))
    assert small.p_value == padded.p_value


def test_the_exact_test_is_used_when_discordant_pairs_are_few():
    """Our pools are tens of items, not thousands. The chi-square approximation
    is unreliable at that size, and using it anyway would report confidence the
    data does not support."""
    result = mcnemar(baseline=_outcomes("0000011111"), candidate=_outcomes("1111111111"))
    assert result.method == "exact"


def test_the_chi_square_approximation_is_used_when_there_are_many():
    baseline = _outcomes("0" * 40 + "1" * 60)
    candidate = _outcomes("1" * 40 + "1" * 60)
    assert mcnemar(baseline=baseline, candidate=candidate).method == "chi-square"


def test_no_discordant_pairs_means_no_evidence_either_way():
    """Not 'the arms are equal' — 'this comparison cannot tell'. Reporting the
    first would be a claim the data does not make."""
    result = mcnemar(baseline=_outcomes("111000"), candidate=_outcomes("111000"))
    assert result.p_value == 1.0
    assert result.favors == "neither"


def test_mismatched_lengths_are_refused():
    """Pairing is the entire basis of the test. Silently zipping to the shorter
    sequence would compare different items and report a number anyway."""
    import pytest

    with pytest.raises(ValueError):
        mcnemar(baseline=[True, False], candidate=[True])


# --- the implementation is checkable against something outside this repo -----


def test_published_values_are_reproduced():
    """Hand-rolled statistics that agree with nothing is a number, not a
    measurement. These are worked examples from the standard reference tables,
    hardcoded so the check runs everywhere — including the laptop install with
    no scientific stack, which is exactly where a skipped check would hide.
    """
    # The textbook 2x2: 17 vs 35 discordant, continuity-corrected chi-square.
    agresti = mcnemar(baseline=[True] * 17 + [False] * 35,
                      candidate=[False] * 17 + [True] * 35)
    assert agresti.method == "chi-square"
    assert round(agresti.p_value, 4) == 0.0184

    # Exact two-sided binomial, 0 vs 12: 2 * (1/2**12).
    clean = mcnemar(baseline=[False] * 12, candidate=[True] * 12)
    assert clean.p_value == 0.00048828125

    # Exact, 2 vs 8 — the classic "a 4-to-1 lead is still not significant".
    lead = mcnemar(baseline=[True] * 2 + [False] * 8,
                   candidate=[False] * 2 + [True] * 8)
    assert lead.p_value == 0.109375
    assert lead.favors == "candidate" and lead.significant is False


def test_agrees_with_scipy_across_the_range():
    """The same check against a real implementation, where one is installed.
    Optional by design: it is corroboration, not the gate."""
    import random

    import pytest

    stats = pytest.importorskip("scipy.stats")

    random.seed(7)
    for _ in range(200):
        n = random.randint(1, 120)
        b = random.randint(0, n)
        c = n - b
        got = mcnemar(baseline=[True] * b + [False] * c,
                      candidate=[False] * b + [True] * c)
        if got.method == "exact":
            want = min(1.0, stats.binomtest(min(b, c), n, 0.5).pvalue)
        else:
            want = stats.chi2.sf(max(0.0, abs(b - c) - 1.0) ** 2 / n, 1)
        assert got.p_value == pytest.approx(want, abs=1e-9)


def test_a_very_small_p_value_survives():
    """Regression: the p-value was rounded to ten decimals, which reads fine at
    p=0.03 and silently reports p=0.0 for a strong result. A probability is read
    in its tail; that is the half rounding destroys."""
    result = mcnemar(baseline=[False] * 60, candidate=[True] * 60)
    assert 0.0 < result.p_value < 1e-13  # ~2.6e-14; the old floor was 1e-10
    assert result.significant is True
