# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the LaTeX quick-code → Unicode expansion table."""

from __future__ import annotations


def test_lowercase_greek():
    from axiom.extensions.builtins.scidisplay.quickcodes import expand_quickcodes

    assert expand_quickcodes(r"\alpha") == "α"
    assert expand_quickcodes(r"\beta") == "β"
    assert expand_quickcodes(r"\pi") == "π"
    assert expand_quickcodes(r"\omega") == "ω"


def test_uppercase_greek():
    from axiom.extensions.builtins.scidisplay.quickcodes import expand_quickcodes

    assert expand_quickcodes(r"\Gamma") == "Γ"
    assert expand_quickcodes(r"\Delta") == "Δ"
    assert expand_quickcodes(r"\Omega") == "Ω"


def test_operators():
    from axiom.extensions.builtins.scidisplay.quickcodes import expand_quickcodes

    assert expand_quickcodes(r"\int") == "∫"
    assert expand_quickcodes(r"\sum") == "∑"
    assert expand_quickcodes(r"\partial") == "∂"
    assert expand_quickcodes(r"\nabla") == "∇"
    assert expand_quickcodes(r"\infty") == "∞"


def test_relations_and_arrows():
    from axiom.extensions.builtins.scidisplay.quickcodes import expand_quickcodes

    assert expand_quickcodes(r"\leq") == "≤"
    assert expand_quickcodes(r"\geq") == "≥"
    assert expand_quickcodes(r"\neq") == "≠"
    assert expand_quickcodes(r"\rightarrow") == "→"
    assert expand_quickcodes(r"\Rightarrow") == "⇒"
    assert expand_quickcodes(r"\iff") == "⇔"


def test_set_theory():
    from axiom.extensions.builtins.scidisplay.quickcodes import expand_quickcodes

    assert expand_quickcodes(r"\in") == "∈"
    assert expand_quickcodes(r"\subset") == "⊂"
    assert expand_quickcodes(r"\cup") == "∪"
    assert expand_quickcodes(r"\forall") == "∀"
    assert expand_quickcodes(r"\exists") == "∃"


def test_blackboard_bold_sets():
    from axiom.extensions.builtins.scidisplay.quickcodes import expand_quickcodes

    assert expand_quickcodes(r"\mathbb{R}") == "ℝ"
    assert expand_quickcodes(r"\mathbb{N}") == "ℕ"
    assert expand_quickcodes(r"\mathbb{Z}") == "ℤ"


def test_inline_substitution_in_sentence():
    from axiom.extensions.builtins.scidisplay.quickcodes import expand_quickcodes

    src = r"For all \alpha \in \mathbb{R}, we have \alpha + 0 = \alpha."
    assert expand_quickcodes(src) == "For all α ∈ ℝ, we have α + 0 = α."


def test_no_shadowing_alpha_vs_alphabet():
    """`\\alphabet` is not a known code, but it MUST NOT match `\\alpha`."""
    from axiom.extensions.builtins.scidisplay.quickcodes import expand_quickcodes

    # alphabet isn't a code — should stay verbatim
    assert expand_quickcodes(r"\alphabet") == r"\alphabet"
    # But \alpha followed by punctuation/space DOES match
    assert expand_quickcodes(r"\alpha bet") == "α bet"
    assert expand_quickcodes(r"\alpha,") == "α,"


def test_unknown_codes_left_alone():
    from axiom.extensions.builtins.scidisplay.quickcodes import expand_quickcodes

    assert expand_quickcodes(r"\unknownmacro") == r"\unknownmacro"
    assert expand_quickcodes(r"\frac{1}{2}") == r"\frac{1}{2}"


def test_idempotent():
    """Running the expansion twice produces the same result."""
    from axiom.extensions.builtins.scidisplay.quickcodes import expand_quickcodes

    src = r"\int_0^\infty e^{-x^2} dx = \frac{\sqrt{\pi}}{2}"
    once = expand_quickcodes(src)
    twice = expand_quickcodes(once)
    assert once == twice


def test_no_backslash_returns_input_unchanged():
    """Fast path — strings with no backslash skip the regex entirely."""
    from axiom.extensions.builtins.scidisplay.quickcodes import expand_quickcodes

    src = "just plain text with no codes 123 + 456"
    assert expand_quickcodes(src) is src or expand_quickcodes(src) == src


def test_table_includes_brand_diversity():
    """Table coverage sanity — at least 50 entries spanning the categories."""
    from axiom.extensions.builtins.scidisplay.quickcodes import known_codes

    table = known_codes()
    assert len(table) >= 50
    # Spot check categories.
    assert r"\alpha" in table
    assert r"\Gamma" in table
    assert r"\int" in table
    assert r"\leq" in table
    assert r"\rightarrow" in table
    assert r"\in" in table
    assert r"\mathbb{R}" in table
