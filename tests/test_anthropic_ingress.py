# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Anthropic /v1/messages ⇄ gateway translation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace

from axiom.llm.anthropic_ingress import (
    anthropic_to_gateway,
    completion_to_anthropic,
    completion_to_openai,
    stream_to_anthropic_sse,
    stream_to_openai_sse,
)

# --- request translation ---------------------------------------------------


def test_system_flatten_and_text_messages():
    body = {
        "system": [{"type": "text", "text": "you are X"}, {"type": "text", "text": "be terse"}],
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    }
    kw = anthropic_to_gateway(body)
    assert kw["system"] == "you are X\nbe terse"
    assert kw["max_tokens"] == 100
    assert kw["messages"] == [{"role": "user", "content": "hi"}]
    assert kw["tools"] is None


def test_tools_mapped_to_openai_shape():
    body = {
        "messages": [{"role": "user", "content": "go"}],
        "tools": [{"name": "search", "description": "find", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}}],
    }
    kw = anthropic_to_gateway(body)
    t = kw["tools"][0]
    assert t["type"] == "function"
    assert t["function"]["name"] == "search"
    assert t["function"]["parameters"]["properties"]["q"]["type"] == "string"


def test_tool_use_and_tool_result_roundtrip():
    body = {
        "messages": [
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "calling"},
                {"type": "tool_use", "id": "tu_1", "name": "wx", "input": {"city": "Austin"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "sunny"},
            ]},
        ]
    }
    msgs = anthropic_to_gateway(body)["messages"]
    # assistant message carries tool_calls
    asst = next(m for m in msgs if m["role"] == "assistant")
    assert asst["tool_calls"][0]["id"] == "tu_1"
    assert json.loads(asst["tool_calls"][0]["function"]["arguments"]) == {"city": "Austin"}
    # tool_result becomes a 'tool' message
    tool_msg = next(m for m in msgs if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "tu_1"
    assert tool_msg["content"] == "sunny"


# --- response translation --------------------------------------------------


@dataclass
class _TU:
    tool_id: str
    name: str
    input: dict = field(default_factory=dict)


def test_completion_text_only():
    resp = SimpleNamespace(text="hello", tool_use=[], stop_reason="", input_tokens=5, output_tokens=2)
    out = completion_to_anthropic(resp, "claude-x")
    assert out["role"] == "assistant" and out["model"] == "claude-x"
    assert out["content"] == [{"type": "text", "text": "hello"}]
    assert out["stop_reason"] == "end_turn"
    assert out["usage"] == {"input_tokens": 5, "output_tokens": 2}


def test_completion_with_tool_use_sets_stop_reason():
    resp = SimpleNamespace(text="", tool_use=[_TU("tu_9", "wx", {"city": "A"})], stop_reason="")
    out = completion_to_anthropic(resp, "m")
    assert out["stop_reason"] == "tool_use"
    assert out["content"][0] == {"type": "tool_use", "id": "tu_9", "name": "wx", "input": {"city": "A"}}


def test_completion_max_tokens_stop_reason():
    resp = SimpleNamespace(text="...", tool_use=[], stop_reason="length")
    assert completion_to_anthropic(resp, "m")["stop_reason"] == "max_tokens"


# --- streaming translation -------------------------------------------------


def _events(chunks):
    raw = "".join(stream_to_anthropic_sse(chunks, "m"))
    return [ln[len("event: "):] for ln in raw.splitlines() if ln.startswith("event: ")]


def test_stream_text_sequence():
    chunks = [
        SimpleNamespace(type="text", text="hel"),
        SimpleNamespace(type="text", text="lo"),
        SimpleNamespace(type="done"),
    ]
    assert _events(chunks) == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]


def test_stream_tool_use_sequence_and_stop_reason():
    chunks = [
        SimpleNamespace(type="text", text="calling"),
        SimpleNamespace(type="tool_use_start", tool_name="wx", tool_id="tu_1"),
        SimpleNamespace(type="tool_input_delta", tool_input_json='{"city":'),
        SimpleNamespace(type="tool_input_delta", tool_input_json='"A"}'),
        SimpleNamespace(type="tool_use_end"),
        SimpleNamespace(type="done"),
    ]
    raw = "".join(stream_to_anthropic_sse(chunks, "m"))
    events = [ln[len("event: "):] for ln in raw.splitlines() if ln.startswith("event: ")]
    # text block then tool block, each opened+closed, then message_delta/stop
    assert events.count("content_block_start") == 2
    assert "input_json_delta" in raw
    # stop_reason flips to tool_use
    assert '"stop_reason": "tool_use"' in raw


# --- routing-policy coordination ------------------------------------------


def test_text_and_context_extracts_last_user_and_prior():
    from axiom.llm.anthropic_ingress import _text_and_context

    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        {"role": "user", "content": [{"type": "text", "text": "current Q"}]},
    ]
    text, context = _text_and_context(msgs)
    assert text == "current Q"
    assert context == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ok"},
    ]


def test_classify_request_uses_shared_router(monkeypatch):
    """The ingress must route through QueryRouter.classify, not a static tier."""
    from axiom.llm import anthropic_ingress as ing

    seen = {}

    class _FakeRouter:
        def classify(self, text, session_mode="auto", context=None):
            seen.update(text=text, session_mode=session_mode, context=context)
            return SimpleNamespace(tier=SimpleNamespace(value="export_controlled"))

    monkeypatch.delenv("AXIOM_BRIDGE_ROUTING_TIER", raising=False)
    body = {"messages": [{"role": "user", "content": "what carrier salt?"}]}
    decision = ing.classify_request(_FakeRouter(), body)
    assert decision.tier.value == "export_controlled"
    assert seen["text"] == "what carrier salt?"
    assert seen["session_mode"] == "auto"  # not pinned -> classify per request


def test_env_pin_forces_export_controlled_session_mode(monkeypatch):
    from axiom.llm import anthropic_ingress as ing

    monkeypatch.setenv("AXIOM_BRIDGE_ROUTING_TIER", "export_controlled")
    assert ing._session_mode() == "export_controlled"
    monkeypatch.delenv("AXIOM_BRIDGE_ROUTING_TIER", raising=False)
    assert ing._session_mode() == "auto"


# --- OpenAI /v1/chat/completions translation -------------------------------


def test_openai_to_gateway_lifts_system_and_passes_tools():
    from axiom.llm.anthropic_ingress import openai_to_gateway

    body = {
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ],
        "tools": [{"type": "function", "function": {"name": "x", "parameters": {}}}],
        "max_tokens": 50,
    }
    kw = openai_to_gateway(body)
    assert kw["system"] == "be terse"
    assert kw["messages"] == [{"role": "user", "content": "hi"}]
    assert kw["tools"][0]["function"]["name"] == "x"
    assert kw["max_tokens"] == 50


def test_completion_to_openai_text_and_tool_calls():
    resp = SimpleNamespace(text="hi", tool_use=[_TU("c1", "wx", {"q": 1})], stop_reason="",
                           input_tokens=3, output_tokens=4)
    out = completion_to_openai(resp, "gpt-x")
    ch = out["choices"][0]
    assert out["object"] == "chat.completion" and out["model"] == "gpt-x"
    assert ch["message"]["content"] == "hi"
    assert ch["message"]["tool_calls"][0]["function"]["name"] == "wx"
    assert ch["finish_reason"] == "tool_calls"
    assert out["usage"]["total_tokens"] == 7


def test_stream_to_openai_sse_shape():
    chunks = [
        SimpleNamespace(type="text", text="he"),
        SimpleNamespace(type="text", text="llo"),
        SimpleNamespace(type="done"),
    ]
    raw = "".join(stream_to_openai_sse(chunks, "m"))
    assert raw.count("chat.completion.chunk") >= 3
    assert '"role": "assistant"' in raw
    assert '"content": "he"' in raw
    assert raw.rstrip().endswith("data: [DONE]")


def test_prefer_from_model():
    from axiom.llm.anthropic_ingress import _prefer_from_model

    names = {"tejas-qwen3-32b", "openrouter"}
    assert _prefer_from_model(names, "tejas-qwen3-32b") == "tejas-qwen3-32b"
    assert _prefer_from_model(names, "claude-sonnet-4") is None


def test_route_path_strips_query_and_slash():
    from axiom.llm.anthropic_ingress import _route_path

    # Claude Code appends ?beta=true — must still route to /v1/messages.
    assert _route_path("/v1/messages?beta=true") == "/v1/messages"
    assert _route_path("/v1/messages/") == "/v1/messages"
    assert _route_path("/v1/messages/count_tokens?beta=true") == "/v1/messages/count_tokens"
    assert _route_path("/health") == "/health"


def test_count_tokens_anthropic():
    from axiom.llm.anthropic_ingress import count_tokens_anthropic

    body = {
        "system": "you are terse",
        "messages": [
            {"role": "user", "content": "hello world"},
            {"role": "assistant", "content": [{"type": "text", "text": "hi there friend"}]},
        ],
    }
    r = count_tokens_anthropic(body)
    assert isinstance(r["input_tokens"], int) and r["input_tokens"] >= 1
    assert count_tokens_anthropic({})["input_tokens"] == 1  # floor of 1, never 0


def test_server_side_tools_stripped_anthropic():
    from axiom.llm.anthropic_ingress import anthropic_to_gateway

    body = {
        "max_tokens": 10,
        "tools": [
            {"name": "Bash", "description": "run", "input_schema": {"type": "object"}},
            {"name": "WebSearch", "description": "search", "input_schema": {"type": "object"}},
            {"name": "WebFetch", "description": "fetch", "input_schema": {"type": "object"}},
        ],
        "messages": [{"role": "user", "content": "hi"}],
    }
    names = [t["function"]["name"] for t in anthropic_to_gateway(body)["tools"]]
    assert names == ["Bash"]  # WebSearch/WebFetch dropped


def test_server_side_tools_stripped_openai():
    from axiom.llm.anthropic_ingress import openai_to_gateway

    body = {
        "max_tokens": 10,
        "tools": [
            {"type": "function", "function": {"name": "Bash", "parameters": {}}},
            {"type": "function", "function": {"name": "WebSearch", "parameters": {}}},
        ],
        "messages": [{"role": "user", "content": "hi"}],
    }
    names = [t["function"]["name"] for t in openai_to_gateway(body)["tools"]]
    assert names == ["Bash"]


def test_strip_tools_env_override(monkeypatch):
    from axiom.llm import anthropic_ingress as ing

    monkeypatch.setenv("AXIOM_BRIDGE_STRIP_TOOLS", "Bash")
    body = {"max_tokens": 10,
            "tools": [{"name": "Bash", "input_schema": {"type": "object"}},
                      {"name": "WebSearch", "input_schema": {"type": "object"}}],
            "messages": [{"role": "user", "content": "hi"}]}
    names = [t["function"]["name"] for t in ing.anthropic_to_gateway(body)["tools"]]
    assert names == ["WebSearch"]  # only Bash stripped now


# --- OpenAI Responses API (/v1/responses) — Codex 0.129+ ---------------------


def test_responses_to_gateway_string_input_and_flat_tools():
    from axiom.llm.anthropic_ingress import responses_to_gateway

    kw = responses_to_gateway({
        "instructions": "be terse",
        "input": "hello",
        "tools": [{"type": "function", "name": "Bash", "description": "run",
                   "parameters": {"type": "object"}}],
        "max_output_tokens": 64,
    })
    assert kw["system"] == "be terse"
    assert kw["messages"] == [{"role": "user", "content": "hello"}]
    # flat Responses tool -> nested chat-completions tool
    assert kw["tools"][0]["function"]["name"] == "Bash"
    assert kw["max_tokens"] == 64


def test_responses_to_gateway_item_list_and_tool_result():
    from axiom.llm.anthropic_ingress import responses_to_gateway

    kw = responses_to_gateway({"input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        {"type": "function_call", "call_id": "c1", "name": "Bash", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "done"},
    ]})
    roles = [m["role"] for m in kw["messages"]]
    assert roles == ["user", "assistant", "tool"]
    assert kw["messages"][1]["tool_calls"][0]["id"] == "c1"
    assert kw["messages"][2]["tool_call_id"] == "c1"


def test_responses_server_side_tools_stripped():
    from axiom.llm.anthropic_ingress import responses_to_gateway

    kw = responses_to_gateway({"input": "hi", "tools": [
        {"type": "function", "name": "Bash", "parameters": {"type": "object"}},
        {"type": "function", "name": "WebSearch", "parameters": {"type": "object"}},
    ]})
    assert [t["function"]["name"] for t in kw["tools"]] == ["Bash"]


def test_completion_to_responses_text_and_toolcall():
    from axiom.llm.anthropic_ingress import completion_to_responses

    out = completion_to_responses(
        SimpleNamespace(text="hi", tool_use=[_TU("c1", "Bash", {"command": "ls"})],
                        input_tokens=3, output_tokens=4), "m")
    assert out["object"] == "response" and out["status"] == "completed"
    types = [o["type"] for o in out["output"]]
    assert "message" in types and "function_call" in types
    assert out["usage"]["total_tokens"] == 7


def test_stream_to_responses_sse_sequence():
    from axiom.llm.anthropic_ingress import stream_to_responses_sse

    raw = "".join(stream_to_responses_sse(
        [SimpleNamespace(type="text", text="O"), SimpleNamespace(type="text", text="K"),
         SimpleNamespace(type="done")], "m"))
    events = [ln[len("event: "):] for ln in raw.splitlines() if ln.startswith("event: ")]
    assert events[0] == "response.created"
    assert "response.output_text.delta" in events
    assert events[-1] == "response.completed"  # Codex requires this terminator


def test_responses_drops_non_function_tools():
    """Codex sends native tool types (web_search/local_shell) with no function
    key; chat-completions 400s on them. They must be dropped; function tools kept."""
    from axiom.llm.anthropic_ingress import responses_to_gateway

    body = {"input": "hi", "tools": [
        {"type": "function", "name": "exec_command", "parameters": {"type": "object"}, "strict": True},
        {"type": "web_search", "external_web_access": True},  # native -> drop
        {"type": "local_shell"},                              # native -> drop
    ]}
    out = responses_to_gateway(body)
    names = [t["function"]["name"] for t in out["tools"]]
    assert names == ["exec_command"]
    assert out["tools"][0]["function"]["strict"] is True
