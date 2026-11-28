# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Cost = uncached input + cache reads x read multiplier + cache writes x write multiplier + output (RS-P11-A1).

Anthropic prices a 5-minute write at 1.25 times the input price and a 1-hour write at 2 times; a read at
0.1. A write the reply does not split by lifetime is priced as a 5-minute write, the default lifetime.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat.usage import TurnUsage, UsageTracker, compute_cost
from tests.routing_acceptance.test_p0 import _record_posts, _Sse, _usage_gateway

SONNET = "claude-sonnet-4-20250514"  # 3.0 in, 15.0 out per 1M tokens


def test_a_five_minute_write_costs_one_and_a_quarter_times_the_input_price():
    cost = compute_cost(SONNET, 0, 0, cache_write=1_000_000, cache_write_5m=1_000_000)
    assert cost == pytest.approx(3.0 * 1.25)


def test_a_one_hour_write_costs_twice_the_input_price():
    cost = compute_cost(SONNET, 0, 0, cache_write=1_000_000, cache_write_1h=1_000_000)
    assert cost == pytest.approx(3.0 * 2)


def test_a_write_with_no_lifetime_split_is_priced_as_a_five_minute_write():
    assert compute_cost(SONNET, 0, 0, cache_write=1_000_000) == pytest.approx(3.0 * 1.25)


def test_a_read_costs_a_tenth_of_the_input_price():
    assert compute_cost(SONNET, 0, 0, cache_read=1_000_000) == pytest.approx(0.3)


def test_all_five_numbers_add_up():
    cost = compute_cost(
        SONNET,
        1000,
        500,
        cache_read=10_000,
        cache_write=3000,
        cache_write_5m=2000,
        cache_write_1h=1000,
    )
    # 1000x3 + 10000x0.3 + 2000x3.75 + 1000x6 + 500x15, per million
    assert cost == pytest.approx(27_000 / 1_000_000)


def test_an_input_count_that_includes_the_cache_is_not_charged_twice():
    # OpenAI format: prompt_tokens (1000) already holds the 400 read tokens; 600 are uncached.
    cost = compute_cost(SONNET, 1000, 0, cache_read=400, input_includes_cache=True)
    assert cost == pytest.approx((600 * 3.0 + 400 * 0.3) / 1_000_000)


def test_a_provider_can_declare_its_own_cache_multipliers():
    p = MagicMock(
        spec=[
            "cost_per_1k_input",
            "cost_per_1k_output",
            "model",
            "cache_read_price",
            "cache_write_price",
        ]
    )
    p.cost_per_1k_input, p.cost_per_1k_output, p.model = 1.0, 2.0, "house-model"
    p.cache_read_price, p.cache_write_price = 0.5, {"5m": 1.0, "1h": 3.0}
    cost = compute_cost(
        "house-model", 0, 0, p, cache_read=1000, cache_write=2000, cache_write_1h=2000
    )
    # $1.00 per 1k input = $1000 per 1M: 1000 reads at 0.5x, 2000 one-hour writes at 3x
    assert cost == pytest.approx((1000 * 1000 * 0.5 + 2000 * 1000 * 3.0) / 1_000_000)


def test_the_tracker_prices_a_turn_that_wrote_to_the_cache():
    tracker = UsageTracker()
    tracker.record_turn(
        TurnUsage(
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=10_000,
            cache_creation_tokens=3000,
            cache_write_5m_tokens=2000,
            cache_write_1h_tokens=1000,
            model=SONNET,
        )
    )
    assert tracker.turns[0].cost == pytest.approx(0.027)


def test_a_streamed_agent_turn_that_wrote_to_the_cache_is_priced_with_the_write(
    monkeypatch, tmp_path
):
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.infra.bus import EventBus
    from axiom.infra.orchestrator.session import Session

    gw = _usage_gateway(monkeypatch, endpoint="https://api.anthropic.com/v1", model=SONNET)
    usage = {
        "input_tokens": 1000,
        "output_tokens": 1,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 2000,
        "cache_creation": {"ephemeral_5m_input_tokens": 2000, "ephemeral_1h_input_tokens": 0},
    }
    events = [
        {"type": "message_start", "message": {"usage": usage}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": usage | {"output_tokens": 500},
        },
        {"type": "message_stop"},
    ]
    _record_posts(monkeypatch, lambda _p: _Sse(events, done=False))
    agent = ChatAgent(gateway=gw, bus=EventBus(log_path=tmp_path / "e.jsonl"), session=Session())
    agent._rag_init_attempted = True
    agent._rag_store = None
    render = MagicMock()
    render.stream_text.side_effect = lambda chunks: "".join(
        c.text for c in chunks if c.type == "text"
    )
    agent.set_render_provider(render)

    agent.turn("what is the value?", stream=True)

    last = agent.usage.turns[-1]
    assert last.cache_creation_tokens == 2000
    assert last.cost == pytest.approx((1000 * 3.0 + 2000 * 3.75 + 500 * 15.0) / 1_000_000)


def test_a_model_with_no_declared_cache_prices_pays_the_input_price_for_cached_tokens():
    """Priced as uncached: the cached tokens are prompt tokens at the input price, never free.

    Anthropic's input count leaves cached tokens out. A model reached under a name the cache table
    does not know (a Bedrock id, say) would otherwise be charged nothing for them.
    """
    from axiom.extensions.builtins.chat.usage import cache_prices_of, compute_cost, price_of

    model = "gpt-4o"
    assert cache_prices_of(model) is None
    p_in, p_out = price_of(model)
    left_out = compute_cost(
        model, 1000, 100, cache_read=400, cache_write=200, input_includes_cache=False
    )
    assert left_out == pytest.approx((1600 * p_in + 100 * p_out) / 1_000_000)
    held = compute_cost(
        model, 1600, 100, cache_read=400, cache_write=200, input_includes_cache=True
    )
    assert held == pytest.approx(left_out)
