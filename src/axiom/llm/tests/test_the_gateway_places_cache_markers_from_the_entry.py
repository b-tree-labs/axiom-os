# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The gateway marks a reusable prefix on an entry that caches only where marked (#2210, RS-P6, RS-P10).

Interim, before the model registry: the cache facts come from the provider entry
(``cache_mode``, ``cache_min_tokens``) or a small built-in table. Off unless
``llm.cache.place_markers`` is on, because a marker on a system part that changes
every turn buys a dearer write that is never read.
"""

from __future__ import annotations

import pytest

from axiom.infra.trace import session_scope
from axiom.llm import cache_markers
from tests.routing_acceptance.test_p0 import _record_posts, _Sse, _usage_gateway

LONG = "rules " * 1200  # about 1,800 tokens by the four-characters rule: above Claude's minimum
REPLY = {
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(cache_markers, "enabled", lambda: True)


def _gateway(monkeypatch, model="claude-sonnet-4-5", **entry):
    gw = _usage_gateway(monkeypatch, endpoint="https://api.anthropic.com/v1", model=model)
    for name, value in entry.items():  # what a provider entry in models.toml would declare
        setattr(gw.providers[0], name, value)
    return gw


def _send(monkeypatch, gw, messages, system=LONG, session="s-1", stream=False):
    sent = _record_posts(monkeypatch, lambda _p: _Sse([], body=REPLY))
    with session_scope(session):
        if stream:
            list(gw._stream_anthropic(gw.providers[0], messages, system, None, 100))
        else:
            gw._call_anthropic_with_tools(gw.providers[0], messages, system, None, 100)
    return sent[-1]


def _marked_system(payload) -> bool:
    system = payload.get("system")
    return isinstance(system, list) and bool(system[-1].get("cache_control"))


def _marked_last_message(payload) -> bool:
    content = payload["messages"][-1]["content"]
    return isinstance(content, list) and bool(content[-1].get("cache_control"))


def _turns(n: int) -> list[dict]:
    out: list[dict] = []
    for i in range(n):
        out.append({"role": "user", "content": f"question {i}"})
        if i < n - 1:
            out.append({"role": "assistant", "content": f"answer {i}"})
    return out


def test_a_session_turn_marks_the_system_part_and_the_end_of_the_history(monkeypatch, on):
    payload = _send(monkeypatch, _gateway(monkeypatch), _turns(3))
    assert _marked_system(payload) and _marked_last_message(payload)
    assert payload["system"][-1]["text"] == LONG


def test_a_streamed_turn_is_marked_the_same_way(monkeypatch, on):
    payload = _send(monkeypatch, _gateway(monkeypatch), _turns(2), stream=True)
    assert _marked_system(payload) and _marked_last_message(payload)


def test_in_a_tool_loop_the_newest_tool_result_is_marked(monkeypatch, on):
    messages = [
        {"role": "user", "content": "look it up"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "t1", "name": "search", "input": {}}],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "found"},
    ]
    payload = _send(monkeypatch, _gateway(monkeypatch), messages, session="")
    assert _marked_last_message(payload)


def test_a_one_shot_call_carries_no_marker(monkeypatch, on):
    # No session and nothing before the question: no reuse is expected (RS-P10-A1).
    payload = _send(monkeypatch, _gateway(monkeypatch), _turns(1), session="")
    assert not _marked_system(payload) and not _marked_last_message(payload)


def test_a_prefix_below_the_minimum_carries_no_marker(monkeypatch, on):
    payload = _send(monkeypatch, _gateway(monkeypatch), _turns(2), system="be brief")
    assert not _marked_system(payload) and not _marked_last_message(payload)


def test_an_entry_can_raise_its_own_minimum(monkeypatch, on):
    payload = _send(monkeypatch, _gateway(monkeypatch, cache_min_tokens=100_000), _turns(2))
    assert not _marked_system(payload)


def test_an_entry_that_caches_by_itself_is_not_marked(monkeypatch, on):
    payload = _send(monkeypatch, _gateway(monkeypatch, cache_mode="automatic"), _turns(2))
    assert not _marked_system(payload) and not _marked_last_message(payload)


def test_a_model_nobody_declared_is_not_marked(monkeypatch, on):
    payload = _send(monkeypatch, _gateway(monkeypatch, model="some-other-model"), _turns(2))
    assert not _marked_system(payload) and not _marked_last_message(payload)


def test_the_callers_own_markers_are_kept_and_none_are_added(monkeypatch, on):
    messages = _turns(2)
    messages[0] = {**messages[0], "cache_control": {"type": "ephemeral"}}
    payload = _send(monkeypatch, _gateway(monkeypatch), messages)
    assert not _marked_system(payload)
    assert payload["messages"][0]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert not _marked_last_message(payload)


def test_nothing_is_marked_while_the_setting_is_off(monkeypatch):
    monkeypatch.setattr(cache_markers, "enabled", lambda: False)
    payload = _send(monkeypatch, _gateway(monkeypatch), _turns(3))
    assert not _marked_system(payload) and not _marked_last_message(payload)


def test_the_setting_ships_off(monkeypatch, tmp_path):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    assert cache_markers.enabled() is False
