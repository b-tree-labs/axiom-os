# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One chat turn cannot fan out or swell without bound (ADR-157 Phase 0.3).

Rounds were capped while calls per round and bytes per result were not, so
rounds × calls × bytes was still unbounded — and the message trimmer keeps the
newest message however large it is, so one oversized tool result could swallow
the whole context budget.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from axiom.extensions.builtins.chat.agent import (
    MAX_TOOL_CALLS_PER_ROUND,
    MAX_TOOL_RESULT_BYTES,
    ChatAgent,
    _bounded_result_json,
)
from axiom.infra.bus import EventBus
from axiom.infra.gateway import CompletionResponse, Gateway, ToolUseBlock
from axiom.infra.orchestrator.session import Session


@pytest.fixture
def agent(tmp_path):
    gw = MagicMock(spec=Gateway)
    gw.available = True
    bus = EventBus(log_path=tmp_path / "events.jsonl")
    return ChatAgent(gateway=gw, bus=bus, session=Session())


def test_a_round_executes_at_most_the_call_cap(agent):
    dispatched = []

    def fake_dispatch(tool_name, args, principal, eventbus, dispatcher, ext_origin):
        dispatched.append(tool_name)
        return {"ok": True}

    n = MAX_TOOL_CALLS_PER_ROUND + 4
    response = CompletionResponse(
        text="",
        tool_use=[
            ToolUseBlock(tool_id=f"t{i}", name="query_docs", input={})
            for i in range(n)
        ],
        provider="test",
        model="test",
        success=True,
    )
    with patch("axiom.infra.tool_gateway.dispatch_tool", side_effect=fake_dispatch):
        results = agent._process_tool_calls(response)

    assert len(dispatched) == MAX_TOOL_CALLS_PER_ROUND
    assert len(results) == n, "every call gets an answer, executed or not"
    refused = [r for _, _, r in results if "tool budget" in str(r.get("error", ""))]
    assert len(refused) == 4


def test_the_cap_does_not_touch_an_ordinary_round(agent):
    dispatched = []

    def fake_dispatch(tool_name, args, principal, eventbus, dispatcher, ext_origin):
        dispatched.append(tool_name)
        return {"ok": True}

    response = CompletionResponse(
        text="",
        tool_use=[
            ToolUseBlock(tool_id=f"t{i}", name="query_docs", input={})
            for i in range(3)
        ],
        provider="test",
        model="test",
        success=True,
    )
    with patch("axiom.infra.tool_gateway.dispatch_tool", side_effect=fake_dispatch):
        results = agent._process_tool_calls(response)
    assert len(dispatched) == 3
    assert not any("error" in r for _, _, r in results)


def test_an_oversized_dict_result_is_reduced_not_passed_whole():
    big = {"data": {"series": [{"t": i, "value": float(i)} for i in range(100_000)]}}
    text = _bounded_result_json(big)
    assert len(text) <= MAX_TOOL_RESULT_BYTES
    out = json.loads(text)
    pts = out["data"]["series"]
    assert pts[0]["t"] == 0 and pts[-1]["t"] == 99_999, (
        "the reduction spans the whole result, never its oldest slice"
    )
    assert any("reduced" in n for n in out.get("notes", []))


def test_an_irreducible_oversized_dict_is_cut_to_its_envelope_with_a_note():
    blob = {"data": "x" * (MAX_TOOL_RESULT_BYTES * 2), "provenance": {"source": "t"}}
    out = json.loads(_bounded_result_json(blob))
    assert out["data"] is None
    assert any("could not be thinned" in n for n in out.get("notes", []))
    assert out["provenance"] == {"source": "t"}, "the envelope survives"


def test_an_oversized_non_dict_result_becomes_an_honest_stub():
    blob = "x" * (MAX_TOOL_RESULT_BYTES * 2)
    out = json.loads(_bounded_result_json(blob))
    assert out["truncated"] is True and "head" in out


def test_a_small_result_is_untouched():
    small = {"ok": True, "value": 7}
    assert json.loads(_bounded_result_json(small)) == small
