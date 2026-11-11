# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The deterministic delta between two answers: counts and sets taken from the texts, no model."""

from __future__ import annotations

from axiom.extensions.builtins.chat.commands_ext.compare import analyze_delta


def test_identical_answers_have_no_delta():
    t = "The pump runs at 40 psi. Check the valve before starting. See [1]."
    d = analyze_delta(t, t)
    assert d.similarity == 1.0
    assert d.matched_sentences == d.sentences_a == 3
    assert d.numeric_disagreements == [] and d.numbers_only_a == [] and d.numbers_only_b == []
    assert d.only_a_terms == [] and d.only_b_terms == []
    assert d.length_ratio == 1.0


def test_numeric_disagreement_pairs_values_for_the_same_thing():
    a = "The maximum operating pressure is 40 psi and the tank holds 120 liters."
    b = "The maximum operating pressure is 55 psi and the tank holds 120 liters."
    d = analyze_delta(a, b)
    assert len(d.numeric_disagreements) == 1
    n = d.numeric_disagreements[0]
    assert (n["a"], n["b"]) == ("40", "55") and "pressure" in n["context"]
    assert d.numbers_only_a == ["40"] and d.numbers_only_b == [
        "55"
    ]  # 120 agrees, so it is not listed


def test_number_formatting_is_not_a_disagreement():
    d = analyze_delta("It costs 1,000 dollars.", "It costs 1000.0 dollars.")
    assert d.numeric_disagreements == [] and d.numbers_only_a == []


def test_list_numbering_and_citation_markers_are_not_numbers():
    a = "1. First step\n2. Second step [3]"
    d = analyze_delta(a, "Different words entirely")
    assert d.numbers_only_a == []
    assert d.citations_a == ["[3]"]


def test_unique_and_shared_terms_and_length_ratio():
    d = analyze_delta("alpha gamma gamma shared", "beta shared shared extra words here now")
    assert "shared" in d.shared_terms
    assert "alpha" in d.only_a_terms and "beta" in d.only_b_terms and "alpha" not in d.only_b_terms
    assert d.words_a == 4 and d.words_b == 7 and d.length_ratio == 7 / 4


def test_citations_each_side_mentions():
    d = analyze_delta("See https://a.example/x and [C1].", "See [C2] and https://a.example/x.")
    assert d.citations_a == ["[C1]", "https://a.example/x"]
    assert d.citations_b == ["[C2]", "https://a.example/x"]


def test_refusal_and_hedging_markers_are_found_per_side():
    d = analyze_delta("I cannot help with that.", "It is probably about 5, perhaps 6.")
    assert d.refusal_a == ["i cannot"] and d.refusal_b == []
    assert d.hedges_b == ["perhaps", "probably"] or set(d.hedges_b) == {"perhaps", "probably"}
    assert d.hedges_a == []


def test_empty_first_answer_has_no_ratio_rather_than_a_division_error():
    assert analyze_delta("", "something").length_ratio is None
