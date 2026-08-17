# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""OpenAI /v1/chat/completions handler.

Pure dict → dict. A backend is a callable `(messages, *, model, **kw) -> str`.
Every call produces one trace on the configured provider.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

from axiom.infra.tracing import NullTraceProvider, TraceProvider

Backend = Callable[..., str]


class ChatCompletionError(ValueError):
    """Raised for malformed requests; transport adapters render as 400."""


class ChatCompletionsHandler:
    """Transport-agnostic handler for OpenAI chat completion requests."""

    def __init__(
        self,
        *,
        backend: Backend,
        trace_provider: TraceProvider | None = None,
    ) -> None:
        self._backend = backend
        self._tracer = trace_provider or NullTraceProvider()

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        messages = request.get("messages")
        if messages is None:
            raise ChatCompletionError("request missing 'messages'")
        if not isinstance(messages, list) or len(messages) == 0:
            raise ChatCompletionError("'messages' must have at least one entry")

        model = request.get("model", "axiom-default")

        trace_id = self._tracer.start_trace(
            "chat.completion", model=model, n_messages=len(messages)
        )

        output = self._backend(messages, model=model, trace_id=trace_id)

        self._tracer.log_generation(
            trace_id, model=model, prompt=messages, output=output
        )
        self._tracer.flush()

        return {
            "id": "chatcmpl-" + uuid.uuid4().hex,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": output},
                    "finish_reason": "stop",
                }
            ],
            "usage": _approx_usage(messages, output),
        }


def _approx_usage(messages: list[dict[str, Any]], output: str) -> dict[str, int]:
    prompt_tokens = sum(
        len(str(m.get("content", "")).split()) for m in messages
    )
    completion_tokens = len(output.split())
    return {
        "prompt_tokens": max(prompt_tokens, 1),
        "completion_tokens": max(completion_tokens, 1),
        "total_tokens": max(prompt_tokens, 1) + max(completion_tokens, 1),
    }
