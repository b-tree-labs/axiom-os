# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""/stats, honest /usage and /status totals, and /context (ADR-168). Real registry, real tracker."""

from __future__ import annotations

import json
import re

import pytest

from axiom.extensions.builtins.chat import builtin_commands, commands_ext  # noqa: F401
from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.extensions.builtins.chat.command_registry import REGISTRY, CommandContext
from axiom.extensions.builtins.chat.usage import TurnUsage
from tests.test_gateway import _gateway_with_providers, _make_provider

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _run(agent, line):
    commands_ext.load_all()
    out = REGISTRY.dispatch(line, CommandContext(agent=agent, store=None, line=line))
    return _ANSI.sub("", out)


@pytest.fixture
def agent(monkeypatch):
    prov = _make_provider(name="cloudy", model="mystery-1", endpoint="https://api.example.com/v1")
    a = ChatAgent(gateway=_gateway_with_providers([prov], monkeypatch))
    return a


def _call(agent, **kw):
    kw.setdefault("input_tokens", 100)
    kw.setdefault("output_tokens", 50)
    agent.usage.record_turn(TurnUsage(**kw))


def test_stats_is_registered_and_in_help():
    commands_ext.load_all()
    assert REGISTRY.get("/stats") is not None
    assert "/stats" in REGISTRY.help_text()


def test_stats_with_no_usage_says_so(agent):
    assert "No usage data" in _run(agent, "/stats")


def test_stats_shows_a_row_per_turn_a_totals_row_and_the_slowest(agent):
    agent.usage.begin_turn()
    _call(agent, model="gpt-4o", provider="alpha", latency_ms=200.0, tool_calls=1)
    agent.usage.begin_turn()
    _call(agent, model="gpt-4o", provider="beta", latency_ms=900.0, tool_calls=0)
    out = _run(agent, "/stats")
    assert "alpha" in out and "beta" in out
    assert "900ms" in out and "200ms" in out
    assert re.search(r"^\s*all\b", out, re.M), out
    assert "Slowest turn: #2" in out


def test_an_untimed_turn_shows_a_dash_and_the_gap_is_named(agent):
    agent.usage.begin_turn()
    _call(agent, model="gpt-4o", provider="alpha")  # no latency, no tool count
    out = _run(agent, "/stats")
    assert "0ms" not in out
    assert "not timed" in out
    assert "Slowest turn" not in out  # nothing was timed, so none can be called slowest


def test_an_unpriced_turn_is_never_dollar_zero_in_stats_usage_or_status(agent):
    agent.usage.begin_turn()
    _call(agent, model="mystery-1", provider="cloudy", latency_ms=5.0, tool_calls=0)
    for line in ("/stats", "/usage", "/status"):
        out = _run(agent, line)
        assert "unpriced" in out, (line, out)
        assert "$0.0000" not in out and "$0.00 " not in out, (line, out)
    assert "1 turn unpriced" in _run(agent, "/usage")


def test_mixed_totals_say_how_many_turns_are_missing(agent):
    agent.usage.begin_turn()
    _call(agent, model="gpt-4o", input_tokens=1000, output_tokens=500, provider="a")
    agent.usage.begin_turn()
    _call(agent, model="mystery-1", provider="b")
    out = _run(agent, "/usage")
    assert "$0.0075 + 1 turn unpriced" in out


def test_a_free_local_turn_is_shown_as_zero_dollars_not_unpriced(monkeypatch):
    prov = _make_provider(name="home", model="m", endpoint="http://localhost:8080/v1", api_key_env="")
    a = ChatAgent(gateway=_gateway_with_providers([prov], monkeypatch))
    a.usage.begin_turn()
    a.usage.record_turn(TurnUsage(input_tokens=5, output_tokens=5, model="m"), provider=prov)
    out = _run(a, "/stats")
    assert "$0.0000" in out and "unpriced" not in out


def test_stats_json_is_the_same_report_for_an_agent_to_read(agent):
    agent.usage.begin_turn()
    _call(agent, model="mystery-1", provider="cloudy", latency_ms=12.0, tool_calls=2)
    rep = json.loads(_run(agent, "/stats --json"))
    t = rep["turns"][0]
    assert (t["provider"] if "provider" in t else t["providers"]) and t["cost"] is None
    assert t["priced"] is False and t["tool_calls"] == 2
    assert rep["totals"]["unpriced_turns"] == 1
    assert rep["totals"]["untimed_turns"] == 0


def test_context_before_anything_is_an_estimate_with_unknown_window(agent):
    out = _run(agent, "/context")
    assert "estimate" in out
    assert "unknown" in out and "128,000" not in out
    assert "Window:" in out


def test_context_uses_the_providers_configured_window(monkeypatch):
    prov = _make_provider(name="p", model="m")
    prov.context_window = 32000
    a = ChatAgent(gateway=_gateway_with_providers([prov], monkeypatch))
    a.session.add_message("user", "x" * 4000)  # ~1000 tokens
    out = _run(a, "/context")
    assert "32,000" in out and "Window: unknown" not in out
    assert "3.1%" in out and "(estimate)" in out


def test_context_shows_overhead_once_a_turn_has_measured_it(agent):
    agent.usage.note_overhead(system_chars=8000, tools_chars=4000, tool_count=7)
    out = _run(agent, "/context")
    assert "System prompt" in out and "~2,000" in out
    assert "Tool schemas" in out and "~1,000" in out and "7 tools" in out


def test_context_shows_the_measured_input_of_the_last_call_separately(agent):
    agent.usage.begin_turn()
    _call(agent, input_tokens=4321, model="gpt-4o", provider="a")
    out = _run(agent, "/context")
    assert "4,321 input tokens (measured" in out
