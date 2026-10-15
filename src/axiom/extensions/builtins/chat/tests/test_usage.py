# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for token usage tracking and cost estimation."""

from axiom.extensions.builtins.chat.usage import (
    CHARS_PER_TOKEN,
    TurnUsage,
    UsageTracker,
    compute_cost,
    estimate_tokens,
)


class TestComputeCost:
    """Test per-model cost computation."""

    def test_known_model(self):
        cost = compute_cost("claude-3-sonnet-20240229", 1000, 500)
        assert cost > 0
        # 1000 * 3.0 / 1M + 500 * 15.0 / 1M = 0.003 + 0.0075 = 0.0105
        assert abs(cost - 0.0105) < 0.001

    def test_unknown_model(self):
        cost = compute_cost("unknown-model-xyz", 1000, 500)
        assert cost == 0.0

    def test_zero_tokens(self):
        cost = compute_cost("gpt-4o", 0, 0)
        assert cost == 0.0

    def test_openai_model(self):
        cost = compute_cost("gpt-4o", 1000, 500)
        assert cost > 0
        # 1000 * 2.5 / 1M + 500 * 10.0 / 1M = 0.0025 + 0.005 = 0.0075
        assert abs(cost - 0.0075) < 0.001

    def test_prefix_matching(self):
        # Model variants should match via prefix
        cost = compute_cost("claude-3-sonnet-20240229-v2", 1000, 500)
        assert cost > 0


class TestEstimateTokens:
    """Test character-based token estimation."""

    def test_basic_estimation(self):
        tokens = estimate_tokens("Hello world!")  # 12 chars
        assert tokens == 12 // CHARS_PER_TOKEN

    def test_empty_string(self):
        tokens = estimate_tokens("")
        assert tokens == 1  # min 1

    def test_long_text(self):
        text = "x" * 4000
        tokens = estimate_tokens(text)
        assert tokens == 1000


class TestTurnUsage:
    """Test TurnUsage serialization."""

    def test_to_dict(self):
        usage = TurnUsage(
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=20,
            model="test-model",
            cost=0.01,
        )
        d = usage.to_dict()
        assert d["input_tokens"] == 100
        assert d["output_tokens"] == 50
        assert d["model"] == "test-model"
        assert d["cost"] == 0.01

    def test_from_dict(self):
        d = {
            "input_tokens": 200,
            "output_tokens": 100,
            "cache_read_tokens": 0,
            "model": "gpt-4o",
            "cost": 0.005,
        }
        usage = TurnUsage.from_dict(d)
        assert usage.input_tokens == 200
        assert usage.model == "gpt-4o"

    def test_from_dict_defaults(self):
        usage = TurnUsage.from_dict({})
        assert usage.input_tokens == 0
        assert usage.model == ""


class TestUsageTracker:
    """Test cumulative usage tracking."""

    def test_empty_tracker(self):
        tracker = UsageTracker()
        assert tracker.total_input_tokens == 0
        assert tracker.total_output_tokens == 0
        assert tracker.total_cost == 0.0
        assert tracker.turn_count == 0

    def test_record_turn(self):
        tracker = UsageTracker()
        tracker.record_turn(
            TurnUsage(
                input_tokens=100,
                output_tokens=50,
                model="gpt-4o",
            )
        )
        assert tracker.total_input_tokens == 100
        assert tracker.total_output_tokens == 50
        assert tracker.turn_count == 1

    def test_auto_compute_cost(self):
        tracker = UsageTracker()
        tracker.record_turn(
            TurnUsage(
                input_tokens=1000,
                output_tokens=500,
                model="gpt-4o",
            )
        )
        assert tracker.turns[0].cost > 0

    def test_multiple_turns(self):
        tracker = UsageTracker()
        tracker.record_turn(TurnUsage(input_tokens=100, output_tokens=50))
        tracker.record_turn(TurnUsage(input_tokens=200, output_tokens=100))
        assert tracker.total_input_tokens == 300
        assert tracker.total_output_tokens == 150
        assert tracker.turn_count == 2

    def test_serialization_roundtrip(self):
        tracker = UsageTracker()
        tracker.record_turn(
            TurnUsage(
                input_tokens=100,
                output_tokens=50,
                model="gpt-4o",
                cost=0.005,
            )
        )
        tracker.record_turn(
            TurnUsage(
                input_tokens=200,
                output_tokens=100,
                model="gpt-4o",
                cost=0.015,
            )
        )

        d = tracker.to_dict()
        assert d["total_input_tokens"] == 300
        assert len(d["turns"]) == 2

        restored = UsageTracker.from_dict(d)
        assert restored.total_input_tokens == 300
        assert restored.total_output_tokens == 150
        assert restored.turn_count == 2

    def test_from_empty_dict(self):
        tracker = UsageTracker.from_dict({})
        assert tracker.turn_count == 0
