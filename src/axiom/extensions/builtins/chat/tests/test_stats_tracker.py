# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The usage tracker keeps what /stats needs: latency, provider, tool calls and the user turn a call belongs to."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat.stats import turn_rows
from axiom.extensions.builtins.chat.usage import TurnUsage, UsageTracker


def _call(t, **kw):
    kw.setdefault("input_tokens", 10)
    kw.setdefault("output_tokens", 5)
    kw.setdefault("model", "gpt-4o")
    t.record_turn(TurnUsage(**kw))


def test_calls_in_one_user_turn_are_one_row():
    t = UsageTracker()
    t.begin_turn()
    _call(t, latency_ms=100.0, tool_calls=2, provider="a")
    _call(t, latency_ms=50.0, tool_calls=0, provider="a")
    t.begin_turn()
    _call(t, latency_ms=10.0, tool_calls=0, provider="b")
    rows = turn_rows(t)
    assert [r.index for r in rows] == [1, 2]
    assert rows[0].calls == 2
    assert rows[0].latency_ms == pytest.approx(150.0)
    assert rows[0].tool_calls == 2
    assert rows[0].input_tokens == 20


def test_a_missing_timing_is_none_never_zero():
    t = UsageTracker()
    t.begin_turn()
    _call(t, latency_ms=100.0)
    _call(t)  # this call was not timed
    assert turn_rows(t)[0].latency_ms is None


def test_unknown_tool_call_count_is_none_never_zero():
    t = UsageTracker()
    t.begin_turn()
    _call(t)
    assert turn_rows(t)[0].tool_calls is None


def test_a_turn_answered_by_two_providers_names_both_in_order():
    t = UsageTracker()
    t.begin_turn()
    _call(t, provider="a")
    _call(t, provider="b")
    _call(t, provider="a")
    assert turn_rows(t)[0].providers == ("a", "b")


def test_a_turn_with_any_unpriced_call_is_unpriced_and_counted_once():
    t = UsageTracker()
    t.begin_turn()
    _call(t, model="mystery")
    _call(t, model="mystery")
    assert turn_rows(t)[0].cost is None
    assert t.unpriced_turns == 1


def test_records_without_begin_turn_are_each_their_own_row():
    t = UsageTracker()
    _call(t)
    _call(t)
    assert [r.index for r in turn_rows(t)] == [1, 2]


def test_new_fields_roundtrip_and_old_dicts_still_load():
    t = UsageTracker()
    t.begin_turn()
    _call(t, latency_ms=12.5, tool_calls=3, provider="a")
    t2 = UsageTracker.from_dict(t.to_dict())
    u = t2.turns[0]
    assert (u.latency_ms, u.tool_calls, u.provider, u.turn) == (12.5, 3, "a", 1)
    legacy = TurnUsage.from_dict({"input_tokens": 1, "output_tokens": 2, "model": "gpt-4o", "cost": 0.1})
    assert legacy.latency_ms is None and legacy.tool_calls is None and legacy.provider == ""


def test_the_agent_records_latency_provider_and_tool_calls(monkeypatch):
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.infra.gateway import CompletionResponse, ToolUseBlock
    from tests.test_gateway import _gateway_with_providers, _make_provider

    prov = _make_provider(name="home", endpoint="http://localhost:8080/v1", api_key_env="", model="m")
    agent = ChatAgent(gateway=_gateway_with_providers([prov], monkeypatch))
    resp = CompletionResponse(
        text="x",
        provider="home",
        model="m",
        input_tokens=7,
        output_tokens=3,
        tool_use=[ToolUseBlock(tool_id="1", name="t"), ToolUseBlock(tool_id="2", name="t")],
    )
    agent._record_usage(resp, latency_ms=42.0)
    u = agent.usage.turns[0]
    assert (u.provider, u.latency_ms, u.tool_calls) == ("home", 42.0, 2)
    assert u.cost == 0.0  # the answering provider is local, so it is explicitly free


def test_the_agent_marks_an_untimed_call_as_untimed(monkeypatch):
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.infra.gateway import CompletionResponse
    from tests.test_gateway import _gateway_with_providers, _make_provider

    prov = _make_provider(name="p", model="m")
    agent = ChatAgent(gateway=_gateway_with_providers([prov], monkeypatch))
    agent._record_usage(CompletionResponse(text="x", provider="p", model="m"))
    assert agent.usage.turns[0].latency_ms is None
