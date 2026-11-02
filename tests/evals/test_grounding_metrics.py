# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Grounding metrics, aligned with published nuclear-domain RAG evaluation.

The metric names are deliberately the ones RADIANT-LLM reports — context
precision, citation precision, citation hit, hallucination rate, retrieval
recall — so a score here is comparable with published numbers rather than a
private dialect nobody can check us against.

The contribution is that one of them need not be judged. A model grading whether
an answer hallucinated is itself a model that can be wrong, and its noise lands
on the metric we care most about. The output-provenance gate already decides,
deterministically, whether every value an answer states is supported by that
turn's evidence — so hallucination rate is computed here, not estimated.
"""

from __future__ import annotations

from axiom.evals.grounding_metrics import (
    GroundingScores,
    citation_hit,
    citation_precision,
    retrieval_recall,
    ungrounded_answer_rate,
)

# --- the deterministic one --------------------------------------------------


def test_hallucination_rate_is_computed_not_judged():
    """A fabricated value and a grounded one, graded by the gate rather than by
    a model asked for its opinion of them."""
    evidence = ["daily peaks: 2.0, 4.0, 6.0"]
    answers = [
        ("The total is $12.00.", ("analytics",)),   # grounded aggregate
        ("The total is $13.50.", ("analytics",)),   # fabricated
    ]
    assert ungrounded_answer_rate(answers, evidence=evidence) == 0.5


def test_a_perfectly_grounded_run_has_a_hallucination_rate_of_zero():
    evidence = ["daily peaks: 2.0, 4.0, 6.0"]
    answers = [("The total is $12.00.", ("analytics",)), ("The mean is $4.00.", ("analytics",))]
    assert ungrounded_answer_rate(answers, evidence=evidence) == 0.0


def test_claiming_a_tool_ran_when_none_did_counts_as_hallucination():
    """False attribution is its own failure: the number may even be right, but
    the claim about where it came from is invented, and that is what a reader
    would rely on."""
    evidence = ["daily peaks: 2.0, 4.0, 6.0"]
    assert ungrounded_answer_rate([("The tool returned $12.00.", ())], evidence=evidence) == 1.0


def test_an_abstention_is_not_a_hallucination():
    """Declining to answer is the behaviour the gate exists to produce. Counting
    it as a hallucination would make the metric argue for switching the gate
    off."""
    assert ungrounded_answer_rate([("I don't have a tool-verified value for that.", ())],
                              evidence=["daily peaks: 2.0, 4.0, 6.0"]) == 0.0


# --- the set-based ones -----------------------------------------------------


def test_retrieval_precision_is_relevant_over_retrieved():
    """Kept as a useful quantity in its own right — it just is not what the
    published CoP measures, so it no longer carries that name."""
    from axiom.evals.grounding_metrics import retrieval_precision

    assert retrieval_precision(retrieved=["a", "b", "c", "d"], relevant={"a", "c"}) == 0.5
    assert retrieval_precision(retrieved=[], relevant={"a"}) == 0.0


def test_citation_precision_is_valid_over_offered():
    """An answer that cites four sources and gets two right is not 100% cited."""
    assert citation_precision(cited=["s1", "s2", "s3", "s4"], valid={"s1", "s3"}) == 0.5


def test_citation_hit_asks_only_whether_any_citation_landed():
    assert citation_hit(cited=["s9", "s1"], valid={"s1"}) == 1.0
    assert citation_hit(cited=["s9"], valid={"s1"}) == 0.0
    assert citation_hit(cited=[], valid={"s1"}) == 0.0


def test_retrieval_recall_is_found_over_all_that_mattered():
    """Generalised from the published visual recall: a figure is one kind of
    thing worth retrieving, and the platform should not privilege one modality
    in a base metric."""
    assert retrieval_recall(retrieved=["a", "b"], relevant={"a", "b", "c", "d"}) == 0.5
    assert retrieval_recall(retrieved=["a"], relevant=set()) == 1.0


def test_negative_control_the_metrics_disagree_with_each_other():
    """Proves they are measuring different things: an answer can cite well and
    still hallucinate, which is the case that motivates reporting all of them."""
    evidence = ["daily peaks: 2.0, 4.0, 6.0"]
    hr = ungrounded_answer_rate([("The total is $99.00.", ("analytics",))], evidence=evidence)
    cip = citation_precision(cited=["s1"], valid={"s1"})
    assert hr == 1.0 and cip == 1.0


def test_scores_bundle_reports_every_metric_together():
    """Reporting one number would let a good citation score hide a bad grounding
    one, which is exactly the trade a benchmark should make visible."""
    scores = GroundingScores(
        context_precision=0.9, citation_precision=0.8,
        citation_hit=1.0, hallucination_rate=0.1, retrieval_recall=0.75,
    )
    d = scores.as_dict()
    assert set(d) == {"CoP", "CiP", "CiH", "HR", "ViR"}
    assert d["HR"] == 0.1


# --- corrected against the published formulas (fetched 2026-09-21) ----------


def test_context_precision_is_blended_correctness_not_retrieval_precision():
    """CoP in the source work is NOT set precision over retrieved chunks. It is
    a correctness score blending an expert semantic grade with a numeric
    accuracy term, alpha 0.6 for mixed queries. The first implementation here
    measured retrieval precision, which is a different quantity that happens to
    share a name."""
    from axiom.evals.grounding_metrics import context_precision

    # all numeric values exact, semantic graded perfect -> 1.0
    assert context_precision(semantic=1.0, predicted=[4.0, 2.0], reference=[4.0, 2.0]) == 1.0
    # semantic perfect, numeric half wrong by 100% -> 0.6*1 + 0.4*0
    assert context_precision(semantic=1.0, predicted=[0.0], reference=[4.0]) == 0.6


def test_the_semantic_grade_is_taken_on_the_published_scale():
    """Discrete 0, 0.25, 0.5, 0.75, 1.0 — an expert grade, not something this
    module computes. Accepting an off-scale value silently would make our number
    incomparable while looking fine."""
    import pytest

    from axiom.evals.grounding_metrics import context_precision

    assert context_precision(semantic=0.75, predicted=[], reference=[]) == 0.75
    with pytest.raises(ValueError):
        context_precision(semantic=0.8, predicted=[], reference=[])


def test_numeric_accuracy_uses_relative_error_against_the_reference():
    from axiom.evals.grounding_metrics import numeric_accuracy

    assert numeric_accuracy(predicted=[4.0], reference=[4.0]) == 1.0
    assert numeric_accuracy(predicted=[2.0], reference=[4.0]) == 0.5   # 1 - 2/4
    assert numeric_accuracy(predicted=[], reference=[]) == 1.0          # nothing to get wrong


def test_a_wildly_wrong_value_floors_at_zero_rather_than_going_negative():
    """1 - |v-v*|/|v*| is unbounded below. A single catastrophic value would
    otherwise drag a whole run's mean negative and make the metric unreadable."""
    from axiom.evals.grounding_metrics import numeric_accuracy

    assert numeric_accuracy(predicted=[1000.0], reference=[1.0]) == 0.0


def test_hallucination_rate_is_claim_level_to_match_the_published_formula():
    """|unsupported claims| / |total claims|, not the share of ANSWERS that
    contained one. An answer with one bad value among ten is not as wrong as an
    answer with ten, and answer-level accounting cannot tell them apart."""
    from axiom.evals.grounding_metrics import hallucination_rate_claims

    assert hallucination_rate_claims(unsupported=1, total=10) == 0.1
    assert hallucination_rate_claims(unsupported=0, total=10) == 0.0
    assert hallucination_rate_claims(unsupported=0, total=0) == 0.0


def test_the_answer_level_rate_is_still_available_but_named_for_what_it_is():
    """Kept because it is what the gate gives directly and it gates a release
    well. Renamed so nobody reports it beside a claim-level published number."""
    from axiom.evals.grounding_metrics import ungrounded_answer_rate

    evidence = ["daily peaks: 2.0, 4.0, 6.0"]
    answers = [("The total is $12.00.", ("analytics",)), ("The total is $13.50.", ("analytics",))]
    assert ungrounded_answer_rate(answers, evidence=evidence) == 0.5
