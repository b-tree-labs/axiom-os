# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A model entry with no declared cache facts is priced as uncached, exactly as before (RS-P11, ADR-272 D1)."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat.usage import TurnUsage, UsageTracker, compute_cost


def test_a_call_with_no_cache_tokens_costs_what_it_always_cost():
    assert compute_cost("gpt-4o", 1000, 500) == pytest.approx(0.0075)
    assert compute_cost("claude-sonnet-4-20250514", 1000, 500) == pytest.approx(0.0105)


def test_cache_tokens_on_a_model_with_no_cache_facts_are_priced_as_plain_input():
    # gpt-4o declares nothing: its 1000 input tokens (read tokens included) cost the input price, as today.
    cost = compute_cost("gpt-4o", 1000, 500, cache_read=400, input_includes_cache=True)
    assert cost == pytest.approx(0.0075)


def test_a_cache_write_already_in_the_input_count_adds_nothing_on_a_model_with_no_cache_facts():
    """OpenAI format: the 1,000 prompt tokens already hold the 300 written, so nothing is added."""
    cost = compute_cost("gpt-4o", 1000, 500, cache_write=300, input_includes_cache=True)
    assert cost == pytest.approx(0.0075)


def test_an_unpriced_model_stays_unpriced_whatever_the_cache_says():
    assert compute_cost("brand-new-model-9", 1000, 500, cache_read=400, cache_write=100) is None


def test_a_turn_with_no_cache_fields_is_priced_as_before_and_says_the_cache_is_unreported():
    tracker = UsageTracker()
    tracker.record_turn(TurnUsage(input_tokens=1000, output_tokens=500, model="gpt-4o"))
    turn = tracker.turns[0]
    assert turn.cost == pytest.approx(0.0075)
    assert turn.cache_read_tokens is None and turn.cache_creation_tokens is None
