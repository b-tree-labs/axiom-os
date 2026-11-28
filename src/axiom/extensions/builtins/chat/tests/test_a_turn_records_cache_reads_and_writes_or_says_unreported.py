# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The usage record carries cache reads, writes and the hit ratio; unreported is None, never 0 (RS-P11, ADR-272 D6)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat.usage import TurnUsage
from tests.routing_acceptance.test_p0 import _record_posts, _Sse, _usage_gateway


def test_a_turn_round_trips_its_cache_reads_and_writes():
    t = TurnUsage(
        input_tokens=100,
        cache_read_tokens=700,
        cache_creation_tokens=200,
        cache_write_5m_tokens=150,
        cache_write_1h_tokens=50,
    )
    back = TurnUsage.from_dict(t.to_dict())
    assert (back.cache_read_tokens, back.cache_creation_tokens) == (700, 200)
    assert (back.cache_write_5m_tokens, back.cache_write_1h_tokens) == (150, 50)


def test_a_record_stored_before_cache_fields_existed_reads_back_as_unreported():
    old = {"input_tokens": 10, "output_tokens": 5, "model": "gpt-4o", "cost": 0.1, "provider": "p"}
    t = TurnUsage.from_dict(old)
    assert t.cache_read_tokens is None and t.cache_creation_tokens is None
    assert t.to_dict()["cache_read_tokens"] is None


def test_the_hit_ratio_is_reads_over_all_prompt_tokens():
    # Anthropic format: input_tokens excludes cache tokens, so the prompt is 100 + 700 + 200.
    t = TurnUsage(input_tokens=100, cache_read_tokens=700, cache_creation_tokens=200)
    assert t.cache_hit_ratio == pytest.approx(0.7)


def test_the_hit_ratio_of_an_input_count_that_includes_the_cache_uses_that_count():
    t = TurnUsage(
        input_tokens=1000, cache_read_tokens=250, cache_creation_tokens=0, input_includes_cache=True
    )
    assert t.cache_hit_ratio == pytest.approx(0.25)


def test_the_hit_ratio_of_an_unreported_turn_is_none_not_zero():
    assert TurnUsage(input_tokens=1000).cache_hit_ratio is None


def _agent(gw, tmp_path):
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.infra.bus import EventBus
    from axiom.infra.orchestrator.session import Session

    agent = ChatAgent(gateway=gw, bus=EventBus(log_path=tmp_path / "e.jsonl"), session=Session())
    agent._rag_init_attempted = True
    agent._rag_store = None
    render = MagicMock()
    render.stream_text.side_effect = lambda chunks: "".join(
        c.text for c in chunks if c.type == "text"
    )
    agent.set_render_provider(render)
    return agent


def _openai_events(usage: dict) -> list:
    return [
        {"choices": [{"index": 0, "delta": {"content": "an answer"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": usage},
    ]


def test_a_streamed_turn_that_reports_reads_and_writes_records_both(monkeypatch, tmp_path):
    gw = _usage_gateway(monkeypatch, endpoint="https://openrouter.example/v1", model="vendor/model")
    details = {"cached_tokens": 256, "cache_write_tokens": 64}
    usage = {"prompt_tokens": 300, "completion_tokens": 20, "prompt_tokens_details": details}
    _record_posts(monkeypatch, lambda p: _Sse(_openai_events(usage)))
    agent = _agent(gw, tmp_path)

    agent.turn("what is the value?", stream=True)

    last = agent.usage.turns[-1]
    assert (last.cache_read_tokens, last.cache_creation_tokens) == (256, 64)
    assert last.input_includes_cache is True


def test_a_streamed_turn_with_no_cache_details_is_unreported_in_the_record(monkeypatch, tmp_path):
    gw = _usage_gateway(monkeypatch, endpoint="http://llm.example.org:8000/v1", model="local-test")
    _record_posts(
        monkeypatch, lambda p: _Sse(_openai_events({"prompt_tokens": 300, "completion_tokens": 20}))
    )
    agent = _agent(gw, tmp_path)

    agent.turn("what is the value?", stream=True)

    last = agent.usage.turns[-1]
    assert last.cache_read_tokens is None and last.cache_creation_tokens is None
