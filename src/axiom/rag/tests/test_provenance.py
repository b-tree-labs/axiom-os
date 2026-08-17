# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the output-provenance gate (post-synthesis fail-closed check)."""

from __future__ import annotations

from axiom.rag.provenance import (
    ProvenanceGateConfig,
    enforce_answer_provenance,
    evaluate_answer_provenance,
)


# --- default (domain-free) config: currency + dates ------------------------


def test_fabricated_provenance_blocked():
    d = evaluate_answer_provenance(
        "I retrieved the worths by calling the pricing tool. The total is $2.50.",
        grounded_texts=[],
        tools_called=[],
    )
    assert not d.grounded
    assert d.false_provenance


def test_unsupported_currency_blocked():
    d = evaluate_answer_provenance("The total is about $2.50.", grounded_texts=[], tools_called=[])
    assert not d.grounded
    assert "$2.50" in d.unsupported


def test_currency_supported_by_corpus_allowed():
    d = evaluate_answer_provenance(
        "The calibrated total is $13.58.",
        grounded_texts=['{"total_worth_dollars": 13.58}'],
        tools_called=[],
    )
    assert d.grounded


def test_provenance_claim_ok_when_a_tool_ran():
    # A real dispatch backs the citation, even with a currency value present.
    d = evaluate_answer_provenance(
        "I retrieved this from the pricing tool: the total is $13.58.",
        grounded_texts=['{"total": 13.58}'],
        tools_called=["pricing"],
    )
    assert d.grounded


def test_bare_date_never_gates_alone():
    d = evaluate_answer_provenance(
        "The outliers fall on 2025-03-10 and 2026-03-23.",
        grounded_texts=[],
        tools_called=[],
    )
    assert d.grounded  # dates are tracked, never failed-closed on their own


def test_empty_answer_is_grounded():
    assert evaluate_answer_provenance("", grounded_texts=[], tools_called=[]).grounded


# --- consumer extension: extra units + authoritative constants -------------

CONSUMER = ProvenanceGateConfig().with_patterns(
    quantity_patterns=(
        r"-?\d+(?:\.\d+)?\s?(?:°\s?C|degrees?\s?C)\b",
        r"-?\d+(?:\.\d+)?\s?(?:kWh|MWh)\b",
    ),
    authoritative_values=("1.1", "960"),
)


def test_consumer_unit_fabrication_blocked():
    d = evaluate_answer_provenance(
        "The measured temperature was 375 °C.", grounded_texts=[], tools_called=[], config=CONSUMER
    )
    assert not d.grounded
    assert any("375" in u for u in d.unsupported)


def test_consumer_authoritative_constant_allowed():
    d = evaluate_answer_provenance(
        "It is rated to 1.1 MWh per cycle.", grounded_texts=[], tools_called=[], config=CONSUMER
    )
    assert d.grounded  # "1.1" is an authoritative constant


def test_consumer_value_echoed_from_prior_context_allowed():
    d = evaluate_answer_provenance(
        "As noted, the value was 375 °C.",
        grounded_texts=["earlier the tool returned 375 °C for that run"],
        tools_called=[],
        config=CONSUMER,
    )
    assert d.grounded  # present in the grounding corpus


def test_consumer_default_currency_still_active():
    # Extending patterns must not drop the inherited currency/date baseline.
    d = evaluate_answer_provenance(
        "$2.50 total.", grounded_texts=[], tools_called=[], config=CONSUMER
    )
    assert not d.grounded


# --- enforcement wrapper ---------------------------------------------------


def test_enforce_substitutes_abstention_on_block():
    ans, dec = enforce_answer_provenance("The total is $2.50.", grounded_texts=[], tools_called=[])
    assert not dec.grounded
    assert ans == ProvenanceGateConfig().abstention
    assert "$" not in ans  # abstention carries no fabricated number


def test_enforce_passes_grounded_answer_through():
    original = "The calibrated total is $13.58."
    ans, dec = enforce_answer_provenance(
        original, grounded_texts=["total 13.58"], tools_called=[]
    )
    assert dec.grounded
    assert ans == original


# --- numeric equivalence: formatting, rounding, honest aggregation ----------
# Live-caught false-positive class (2026-07-16): tool JSON round-trips through
# the serving layer as Python floats (2.90 -> "2.9"), the model formats
# currency conventionally ("$2.90"), rounds full precision ("$3.40" for 3.396),
# and computes honest aggregates ("$13.58" = the sum of the four rods) — all
# blocked by substring matching. Support must be value-based, not string-based.

# The tool result exactly as the serving layer serializes it (float round-trip).
_ROD_TOOL_JSON = (
    '{"data": {"rods": ['
    '{"rod": "Regulating", "total_worth_cents": 383.6, "total_worth_dollars": 3.84}, '
    '{"rod": "Shim1", "total_worth_cents": 290.0, "total_worth_dollars": 2.9}, '
    '{"rod": "Shim2", "total_worth_cents": 344.1, "total_worth_dollars": 3.44}, '
    '{"rod": "Transient", "total_worth_cents": 339.6, "total_worth_dollars": 3.4}]}}'
)
_SCALED = ProvenanceGateConfig(equivalence_scales=(1.0, 100.0, 0.01))


def test_trailing_zero_formatting_supported():
    d = evaluate_answer_provenance(
        "Shim 1 is worth $2.90.", grounded_texts=['{"worth": 2.9}'], tools_called=["t"]
    )
    assert d.grounded


def test_rounded_display_of_full_precision_supported():
    d = evaluate_answer_provenance(
        "The transient rod is worth $3.40.",
        grounded_texts=['{"worth": 3.396}'],
        tools_called=["t"],
    )
    assert d.grounded


def test_honest_sum_and_unit_conversion_supported():
    # $13.58 and "1357.3 cents" appear NOWHERE verbatim: the model summed the
    # per-rod values. Numeric closure over tool-result arrays must support it.
    answer = (
        "The total control rod worth is $13.58 (1357.3 cents). Breakdown: "
        "Regulating $3.84, Shim 1 $2.90, Shim 2 $3.44, Transient $3.40."
    )
    d = evaluate_answer_provenance(
        answer, grounded_texts=[_ROD_TOOL_JSON], tools_called=["reactor_rod_worth"], config=_SCALED
    )
    assert d.grounded, d.reason


def test_fabricated_total_still_blocked_with_tool_result_present():
    # The Drew value against the REAL tool result: equivalence must not launder it.
    d = evaluate_answer_provenance(
        "The total control rod worth is $2.50.",
        grounded_texts=[_ROD_TOOL_JSON],
        tools_called=["reactor_rod_worth"],
        config=_SCALED,
    )
    assert not d.grounded


def test_near_but_wrong_value_still_blocked():
    d = evaluate_answer_provenance(
        "The rod is worth $3.60.", grounded_texts=['{"worth": 3.396}'], tools_called=["t"]
    )
    assert not d.grounded


def test_scale_equivalence_is_config_gated():
    dollars_only = '{"total_worth_dollars": 13.573}'
    blocked = evaluate_answer_provenance(
        "That is 1357.3 cents.", grounded_texts=[dollars_only], tools_called=["t"]
    )
    allowed = evaluate_answer_provenance(
        "That is 1357.3 cents.", grounded_texts=[dollars_only], tools_called=["t"], config=_SCALED
    )
    assert not blocked.grounded
    assert allowed.grounded
