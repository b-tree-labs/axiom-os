# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tool-calling strategy — native-first + shim fallback, self-monitoring."""

from __future__ import annotations

import logging

from axiom.llm.tool_calling import (
    ToolCall,
    parse_shim_response,
    probe_native_support,
    resolve_tool_calls,
    shim_preamble,
)

_TOOLS = [{"function": {"name": "agent_task_status", "description": "what's running",
                        "parameters": {"type": "object", "properties": {}}}},
          {"function": {"name": "agent_stop_task", "description": "stop a task",
                        "parameters": {"type": "object", "properties": {"task_id": {}}}}}]


# --- shim parsing -------------------------------------------------------------

def test_parse_plain_json_action():
    c = parse_shim_response('{"tool": "agent_stop_task", "arguments": {"task_id": "abc"}}')
    assert c == ToolCall("agent_stop_task", {"task_id": "abc"})


def test_parse_json_amid_prose_and_fences():
    text = 'Sure, stopping it.\n```json\n{"tool":"agent_stop_task","arguments":{"task_id":"x"}}\n```'
    c = parse_shim_response(text)
    assert c.name == "agent_stop_task" and c.arguments["task_id"] == "x"


def test_parse_plain_answer_is_none():
    assert parse_shim_response("It's going well, about halfway through.") is None


def test_preamble_lists_tools():
    p = shim_preamble(_TOOLS)
    assert "agent_task_status" in p and "agent_stop_task" in p and "JSON" in p


# --- resolver: native path ----------------------------------------------------

def test_native_calls_used_when_present():
    res = resolve_tool_calls(
        _TOOLS,
        native_fn=lambda: ([ToolCall("agent_task_status", {})], ""),
        complete_fn=lambda sysmsg: (_ for _ in ()).throw(AssertionError("shim must not run")),
        mode="auto", provider="anthropic",
    )
    assert res.mode_used == "native" and res.calls[0].name == "agent_task_status"


# --- resolver: shim fallback (the Qwen case) ----------------------------------

def test_auto_falls_back_to_shim_when_native_empty():
    res = resolve_tool_calls(
        _TOOLS,
        native_fn=lambda: ([], "I should check the tasks..."),  # prose, no tool_calls
        complete_fn=lambda sysmsg: '{"tool": "agent_task_status", "arguments": {}}',
        mode="auto", provider="qwen-selfhosted",
    )
    assert res.mode_used == "shim" and res.calls[0].name == "agent_task_status"
    assert res.anomaly is None  # expected for a genuinely non-tool-calling provider


def test_plain_answer_when_neither_path_calls():
    res = resolve_tool_calls(
        _TOOLS,
        native_fn=lambda: ([], ""),
        complete_fn=lambda sysmsg: "It's going smoothly.",
        mode="auto", provider="qwen-selfhosted",
    )
    assert res.mode_used == "none" and "smoothly" in res.text


# --- self-monitoring ----------------------------------------------------------

def test_native_declared_but_empty_alerts_and_uses_shim(caplog):
    with caplog.at_level(logging.WARNING):
        res = resolve_tool_calls(
            _TOOLS,
            native_fn=lambda: ([], "I'll call agent_task_status now"),  # names a tool in prose
            complete_fn=lambda sysmsg: '{"tool":"agent_task_status","arguments":{}}',
            mode="native", provider="brokenserver",
        )
    assert res.mode_used == "shim" and res.anomaly and "misconfigured" in res.anomaly
    assert any("TOOL_CALLING_ALERT" in r.message for r in caplog.records)


def test_shim_mode_is_fast_path_no_native_probe():
    # shim mode must NOT call native (saves a wasted round-trip on CPU models).
    def _native():
        raise AssertionError("shim mode must not probe native per-turn")
    res = resolve_tool_calls(
        _TOOLS, native_fn=_native,
        complete_fn=lambda sysmsg: '{"tool":"agent_task_status","arguments":{}}',
        mode="shim", provider="qwen-selfhosted",
    )
    assert res.mode_used == "shim"


def test_probe_native_support_flags_outdated_shim(caplog):
    # out-of-band staleness check: native now works → alert to flip the pin.
    with caplog.at_level(logging.WARNING):
        works = probe_native_support(
            lambda: ([ToolCall("agent_task_status", {})], ""), provider="qwen-selfhosted")
    assert works is True
    assert any("outdated" in r.message and "TOOL_CALLING_ALERT" in r.message for r in caplog.records)
