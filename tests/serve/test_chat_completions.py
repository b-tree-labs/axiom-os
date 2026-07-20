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
    h = ChatCompletionsHandler(
        backend=lambda messages, **_: "hi", trace_provider=tracer
    )
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
