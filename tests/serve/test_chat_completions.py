# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""OpenAI-compatible /v1/chat/completions handler (transport-agnostic).

Handlers are pure functions mapping dict → dict in the OpenAI format.
A FastAPI/Starlette adapter lives separately and only does request/response
marshaling; all business logic is here and testable without a server.
"""

from __future__ import annotations


def _req(messages, model="axiom-default", **extra):
    return {"model": model, "messages": messages, **extra}


def test_handler_returns_openai_shaped_response() -> None:
    from axiom.serve import ChatCompletionsHandler

    h = ChatCompletionsHandler(backend=lambda messages, **_: "hello")
    resp = h.handle(_req([{"role": "user", "content": "hi"}]))

    assert resp["object"] == "chat.completion"
    assert resp["model"] == "axiom-default"
    assert "id" in resp
    assert "created" in resp
    assert resp["choices"][0]["index"] == 0
    assert resp["choices"][0]["message"]["role"] == "assistant"
    assert resp["choices"][0]["message"]["content"] == "hello"
    assert resp["choices"][0]["finish_reason"] == "stop"


def test_handler_forwards_message_history_to_backend() -> None:
    from axiom.serve import ChatCompletionsHandler

    seen: list = []

    def backend(messages, **_):
        seen.extend(messages)
        return "ok"

    h = ChatCompletionsHandler(backend=backend)
    h.handle(
        _req(
            [
                {"role": "system", "content": "you are helpful"},
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"},
            ]
        )
    )
    assert [m["role"] for m in seen] == ["system", "user", "assistant", "user"]


def test_handler_rejects_missing_messages() -> None:
    import pytest

    from axiom.serve import ChatCompletionError, ChatCompletionsHandler

    h = ChatCompletionsHandler(backend=lambda messages, **_: "")
    with pytest.raises(ChatCompletionError, match="messages"):
        h.handle({"model": "m"})


def test_handler_rejects_empty_messages() -> None:
    import pytest

    from axiom.serve import ChatCompletionError, ChatCompletionsHandler

    h = ChatCompletionsHandler(backend=lambda messages, **_: "")
    with pytest.raises(ChatCompletionError, match="at least one"):
        h.handle(_req([]))


def test_handler_emits_usage_counts() -> None:
    from axiom.serve import ChatCompletionsHandler

    h = ChatCompletionsHandler(backend=lambda messages, **_: "hello world")
    resp = h.handle(_req([{"role": "user", "content": "hi there"}]))

    usage = resp["usage"]
    # Approximate token counts — whitespace split is fine for the contract.
    assert usage["prompt_tokens"] >= 1
    assert usage["completion_tokens"] >= 1
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def test_handler_traces_to_provider() -> None:
    from axiom.infra.tracing import InMemoryTraceProvider
    from axiom.serve import ChatCompletionsHandler

    tracer = InMemoryTraceProvider()
    h = ChatCompletionsHandler(backend=lambda messages, **_: "hi", trace_provider=tracer)
    h.handle(_req([{"role": "user", "content": "hello"}]))

    assert len(tracer.traces) == 1
    assert tracer.traces[0]["name"] == "chat.completion"
    assert len(tracer.generations) == 1
    assert tracer.generations[0]["model"] == "axiom-default"


def test_handler_preserves_model_field() -> None:
    from axiom.serve import ChatCompletionsHandler

    h = ChatCompletionsHandler(backend=lambda messages, **_: "hi")
    resp = h.handle(_req([{"role": "user", "content": "x"}], model="bonsai-1.7b"))
    assert resp["model"] == "bonsai-1.7b"


def test_backend_can_dispatch_on_model() -> None:
    from axiom.serve import ChatCompletionsHandler

    def backend(messages, *, model, **_):
        return f"routed-to-{model}"

    h = ChatCompletionsHandler(backend=backend)
    resp = h.handle(_req([{"role": "user", "content": "x"}], model="qwen-122b"))
    assert resp["choices"][0]["message"]["content"] == "routed-to-qwen-122b"


# ---------------------------------------------------------------------------
# Legacy contract pin — a plain string backend must keep producing EXACTLY the
# response shape it produced before streaming/tool-calls/errors were added.
# ---------------------------------------------------------------------------


def test_legacy_string_backend_response_shape_is_pinned() -> None:
    from axiom.serve import ChatCompletionsHandler

    h = ChatCompletionsHandler(backend=lambda messages, **_: "hello world")
    resp = h.handle(_req([{"role": "user", "content": "hi there"}]))

    assert set(resp) == {"id", "object", "created", "model", "choices", "usage"}
    assert resp["id"].startswith("chatcmpl-")
    assert resp["object"] == "chat.completion"
    assert isinstance(resp["created"], int)
    assert resp["model"] == "axiom-default"
    assert len(resp["choices"]) == 1
    assert set(resp["choices"][0]) == {"index", "message", "finish_reason"}
    # No stray "tool_calls" key on an ordinary turn.
    assert resp["choices"][0]["message"] == {"role": "assistant", "content": "hello world"}
    assert resp["choices"][0]["finish_reason"] == "stop"
    assert resp["usage"] == {
        "prompt_tokens": 2,
        "completion_tokens": 2,
        "total_tokens": 4,
    }


def test_legacy_backend_call_shape_is_unchanged_without_optional_fields() -> None:
    """Absent request fields are not synthesised into the backend call."""
    from axiom.serve import ChatCompletionsHandler

    seen: dict = {}

    def backend(messages, **kwargs):
        seen.update(kwargs)
        return "ok"

    ChatCompletionsHandler(backend=backend).handle(_req([{"role": "user", "content": "x"}]))
    assert set(seen) == {"model", "trace_id"}


def test_optional_openai_fields_are_forwarded_when_present() -> None:
    from axiom.serve import ChatCompletionsHandler

    seen: dict = {}

    def backend(messages, **kwargs):
        seen.update(kwargs)
        return "ok"

    ChatCompletionsHandler(backend=backend).handle(
        _req(
            [{"role": "user", "content": "x"}],
            max_tokens=16,
            temperature=0.2,
            tools=[{"type": "function"}],
        )
    )
    assert seen["max_tokens"] == 16
    assert seen["temperature"] == 0.2
    assert seen["tools"] == [{"type": "function"}]


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


class _StreamingBackend:
    """A backend that can emit incremental deltas."""

    def __init__(self, pieces):
        self.pieces = list(pieces)
        self.stream_calls = 0
        self.call_calls = 0

    def __call__(self, messages, **_):
        self.call_calls += 1
        return "".join(p for p in self.pieces if isinstance(p, str))

    def stream(self, messages, **_):
        self.stream_calls += 1
        yield from self.pieces


def test_stream_emits_role_then_content_then_finish() -> None:
    from axiom.serve import ChatCompletionsHandler

    backend = _StreamingBackend(["Hel", "lo ", "world"])
    h = ChatCompletionsHandler(backend=backend)
    chunks = list(h.handle_stream(_req([{"role": "user", "content": "hi"}])))

    assert backend.stream_calls == 1
    assert backend.call_calls == 0
    assert [c["object"] for c in chunks] == ["chat.completion.chunk"] * 5
    # One id and one created timestamp for the whole response.
    assert len({c["id"] for c in chunks}) == 1
    assert len({c["created"] for c in chunks}) == 1

    deltas = [c["choices"][0]["delta"] for c in chunks]
    assert deltas[0] == {"role": "assistant"}
    assert [d["content"] for d in deltas[1:4]] == ["Hel", "lo ", "world"]
    assert deltas[4] == {}

    finishes = [c["choices"][0]["finish_reason"] for c in chunks]
    assert finishes == [None, None, None, None, "stop"]


def test_non_streaming_backend_falls_back_to_one_content_chunk() -> None:
    from axiom.serve import ChatCompletionsHandler

    h = ChatCompletionsHandler(backend=lambda messages, **_: "whole message")
    chunks = list(h.handle_stream(_req([{"role": "user", "content": "hi"}])))

    assert len(chunks) == 3
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert chunks[1]["choices"][0]["delta"] == {"content": "whole message"}
    assert chunks[2]["choices"][0]["finish_reason"] == "stop"


def test_stream_validates_the_request_eagerly() -> None:
    import pytest

    from axiom.serve import ChatCompletionError, ChatCompletionsHandler

    h = ChatCompletionsHandler(backend=lambda messages, **_: "x")
    # Raises on the call itself, not on the first next() of the iterator.
    with pytest.raises(ChatCompletionError, match="messages"):
        h.handle_stream({"model": "m"})


def test_stream_traces_the_assembled_completion() -> None:
    from axiom.infra.tracing import InMemoryTraceProvider
    from axiom.serve import ChatCompletionsHandler

    tracer = InMemoryTraceProvider()
    h = ChatCompletionsHandler(backend=_StreamingBackend(["a", "b"]), trace_provider=tracer)
    list(h.handle_stream(_req([{"role": "user", "content": "hi"}])))

    assert tracer.generations[0]["output"] == "ab"
    assert tracer.generations[0]["metadata"]["finish_reason"] == "stop"
    assert tracer.traces[0]["metadata"]["stream"] is True


def test_backend_streams_capability_flag() -> None:
    from axiom.serve import ChatCompletionsHandler

    assert ChatCompletionsHandler(backend=_StreamingBackend(["a"])).backend_streams
    assert not ChatCompletionsHandler(backend=lambda messages, **_: "a").backend_streams


# ---------------------------------------------------------------------------
# SSE rendering
# ---------------------------------------------------------------------------


def test_render_sse_frames_chunks_and_terminates_with_done() -> None:
    import json

    from axiom.serve import ChatCompletionsHandler, render_sse

    h = ChatCompletionsHandler(backend=_StreamingBackend(["hi"]))
    lines = list(render_sse(h.handle_stream(_req([{"role": "user", "content": "q"}]))))

    assert all(line.startswith("data: ") and line.endswith("\n\n") for line in lines)
    assert lines[-1] == "data: [DONE]\n\n"
    payloads = [json.loads(line[len("data: ") :].strip()) for line in lines[:-1]]
    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"


def test_render_sse_of_empty_stream_is_just_the_terminator() -> None:
    from axiom.serve import render_sse

    assert list(render_sse([])) == ["data: [DONE]\n\n"]


# ---------------------------------------------------------------------------
# Tool calls — reported, never executed (single-call serving path).
# ---------------------------------------------------------------------------


_TOOL_CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "lookup", "arguments": '{"q": "x"}'},
}


def test_tool_calls_are_emitted_with_tool_calls_finish_reason() -> None:
    from axiom.serve import BackendResult, ChatCompletionsHandler

    h = ChatCompletionsHandler(backend=lambda messages, **_: BackendResult(tool_calls=[_TOOL_CALL]))
    resp = h.handle(_req([{"role": "user", "content": "look it up"}]))

    choice = resp["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"] == [_TOOL_CALL]


def test_tool_calls_can_accompany_content() -> None:
    from axiom.serve import BackendResult, ChatCompletionsHandler

    h = ChatCompletionsHandler(
        backend=lambda messages, **_: BackendResult(content="checking", tool_calls=[_TOOL_CALL])
    )
    choice = h.handle(_req([{"role": "user", "content": "x"}]))["choices"][0]
    assert choice["message"]["content"] == "checking"
    assert choice["finish_reason"] == "tool_calls"


def test_tool_calls_stream_as_an_indexed_delta() -> None:
    from axiom.serve import BackendResult, ChatCompletionsHandler

    backend = _StreamingBackend([BackendResult(tool_calls=[_TOOL_CALL, _TOOL_CALL])])
    chunks = list(
        ChatCompletionsHandler(backend=backend).handle_stream(
            _req([{"role": "user", "content": "x"}])
        )
    )

    tool_delta = chunks[1]["choices"][0]["delta"]["tool_calls"]
    assert [tc["index"] for tc in tool_delta] == [0, 1]
    assert tool_delta[0]["id"] == "call_1"
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_backend_result_is_frozen_and_normalises_tool_calls() -> None:
    import dataclasses

    import pytest

    from axiom.serve import BackendResult

    r = BackendResult(content="x", tool_calls=[_TOOL_CALL])
    assert isinstance(r.tool_calls, tuple)
    assert dataclasses.is_dataclass(r)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.content = "y"


# ---------------------------------------------------------------------------
# finish_reason: length
# ---------------------------------------------------------------------------


def test_truncated_backend_reports_length_finish_reason() -> None:
    from axiom.serve import BackendResult, ChatCompletionsHandler

    def backend(messages, *, max_tokens=None, **_):
        return BackendResult(content="a b c", truncated=max_tokens is not None)

    h = ChatCompletionsHandler(backend=backend)
    resp = h.handle(_req([{"role": "user", "content": "x"}], max_tokens=3))
    assert resp["choices"][0]["finish_reason"] == "length"

    # No max_tokens -> the backend does not truncate -> default "stop".
    resp2 = h.handle(_req([{"role": "user", "content": "x"}]))
    assert resp2["choices"][0]["finish_reason"] == "stop"


def test_truncation_survives_the_streaming_path() -> None:
    from axiom.serve import BackendResult, ChatCompletionsHandler

    backend = _StreamingBackend(["a ", BackendResult(content="b", truncated=True)])
    chunks = list(
        ChatCompletionsHandler(backend=backend).handle_stream(
            _req([{"role": "user", "content": "x"}])
        )
    )
    assert chunks[-1]["choices"][0]["finish_reason"] == "length"


def test_backend_returning_a_bad_type_raises_type_error() -> None:
    import pytest

    from axiom.serve import ChatCompletionsHandler

    h = ChatCompletionsHandler(backend=lambda messages, **_: 42)
    with pytest.raises(TypeError, match="expected str or BackendResult"):
        h.handle(_req([{"role": "user", "content": "x"}]))


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


def test_error_renders_an_openai_shaped_envelope_with_a_status_code() -> None:
    from axiom.serve import ChatCompletionError

    err = ChatCompletionError(
        "bad model", type="not_found_error", param="model", code="model_not_found", status_code=404
    )
    assert err.status_code == 404
    assert err.to_error_response() == {
        "error": {
            "message": "bad model",
            "type": "not_found_error",
            "param": "model",
            "code": "model_not_found",
        }
    }


def test_validation_errors_carry_a_400_envelope() -> None:
    import pytest

    from axiom.serve import ChatCompletionError, ChatCompletionsHandler

    h = ChatCompletionsHandler(backend=lambda messages, **_: "")
    with pytest.raises(ChatCompletionError) as exc:
        h.handle({"model": "m"})

    err = exc.value
    assert err.status_code == 400
    envelope = err.to_error_response()["error"]
    assert envelope["type"] == "invalid_request_error"
    assert envelope["param"] == "messages"
    assert "messages" in envelope["message"]
    # Still a ValueError with a plain str() message for existing callers.
    assert isinstance(err, ValueError)
    assert str(err) == envelope["message"]


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


def test_estimate_usage_is_deterministic_and_floors_at_one() -> None:
    from axiom.serve import estimate_usage

    messages = [{"role": "user", "content": "one two three"}]
    first = estimate_usage(messages, "four five")
    assert first == estimate_usage(messages, "four five")
    assert first == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    # Empty prompt and completion still report at least one token each.
    assert estimate_usage([{"role": "user", "content": ""}], "") == {
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
    }


def test_usage_accounts_for_tool_call_payloads() -> None:
    from axiom.serve import BackendResult, ChatCompletionsHandler

    h = ChatCompletionsHandler(backend=lambda messages, **_: BackendResult(tool_calls=[_TOOL_CALL]))
    resp = h.handle(_req([{"role": "user", "content": "x"}]))
    assert resp["usage"]["completion_tokens"] > 1


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_plain_function_and_streaming_object_satisfy_the_backend_protocol() -> None:
    from axiom.serve import ChatBackend, StreamingChatBackend

    def plain(messages, *, model, trace_id, **kwargs):
        return "x"

    streamer = _StreamingBackend(["x"])

    assert isinstance(plain, ChatBackend)
    assert isinstance(streamer, ChatBackend)
    # Only the object exposing .stream conforms to the streaming refinement.
    assert not isinstance(plain, StreamingChatBackend)
    assert isinstance(streamer, StreamingChatBackend)
    assert not isinstance("not a backend", ChatBackend)
