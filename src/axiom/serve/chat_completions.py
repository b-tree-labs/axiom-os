# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""OpenAI ``/v1/chat/completions`` handler — the serving *transport*.

Pure ``dict -> dict`` (and ``dict -> Iterator[dict]`` for streaming), so any
transport (FastAPI, Starlette, a unit test) can drive it. This module
deliberately mounts nothing and imports no web framework: an adapter owns
request/response marshaling, this owns the OpenAI wire contract.

**Serving-path constraint (2026-06-29 incident).** The sanctioned serving path
is ``retrieve -> inject -> call``: a SINGLE model call per request. It does NOT
run an agent loop. This handler is the transport for that path, so an
agent-backed backend is an explicit, bounded opt-in rather than the default:

* The default backend is a plain callable returning ``str`` — one call in, one
  message out. That path is byte-for-byte what it has always been.
* A backend opts into tool semantics only by *returning* a :class:`BackendResult`
  carrying ``tool_calls``. The handler then *reports* them to the caller
  (``finish_reason="tool_calls"``) and stops. It never executes a tool, never
  re-invokes the backend, and never loops. Continuing the exchange is the
  client's decision, on the client's turn budget.

Backend contract, in one line: ``backend(messages, *, model, trace_id, **kw)``
returns ``str`` or :class:`BackendResult`; an optional ``backend.stream(...)``
yields the same pieces incrementally. Capability is detected structurally
(:class:`StreamingChatBackend`), never by calling and catching.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from axiom.infra.tracing import NullTraceProvider, TraceProvider

# Request fields forwarded to the backend when (and only when) the caller sent
# them. Absent keys are not synthesised, so the default call shape stays
# ``backend(messages, model=..., trace_id=...)`` exactly as it always was.
_FORWARDED_REQUEST_FIELDS = ("max_tokens", "temperature", "tools", "tool_choice")


@dataclass(frozen=True)
class BackendResult:
    """A structured backend return value.

    Backends may keep returning a bare ``str``; this is the typed opt-in for
    anything richer.

    Attributes:
        content: Assistant message content. ``None`` for a tool-call-only turn.
        tool_calls: OpenAI ``tool_calls`` entries. Present ⇒ ``finish_reason``
            becomes ``"tool_calls"`` and the handler hands them back unexecuted.
        truncated: The backend stopped because it hit the request's
            ``max_tokens`` (or its own cap) ⇒ ``finish_reason`` becomes
            ``"length"``.
    """

    content: str | None = None
    tool_calls: Sequence[dict[str, Any]] | None = None
    truncated: bool = False

    def __post_init__(self) -> None:
        # Normalise to a tuple so the frozen dataclass is actually immutable.
        if self.tool_calls is not None:
            object.__setattr__(self, "tool_calls", tuple(self.tool_calls))

    @property
    def finish_reason(self) -> str:
        """OpenAI ``finish_reason`` implied by this result."""
        if self.tool_calls:
            return "tool_calls"
        if self.truncated:
            return "length"
        return "stop"

    @property
    def text(self) -> str:
        """Content as a string (``""`` when the turn is tool calls only)."""
        return self.content or ""


@runtime_checkable
class ChatBackend(Protocol):
    """The backend contract: messages in, one message (or tool calls) out.

    Any plain callable of this shape conforms — no base class, no registration.
    """

    def __call__(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        trace_id: str,
        **kwargs: Any,
    ) -> str | BackendResult: ...


@runtime_checkable
class StreamingChatBackend(ChatBackend, Protocol):
    """A :class:`ChatBackend` that can also emit its answer incrementally.

    ``stream`` yields ``str`` pieces (content deltas) and/or
    :class:`BackendResult` pieces (content, tool calls, truncation). Backends
    without a ``stream`` attribute are still fully usable through
    :meth:`ChatCompletionsHandler.handle_stream`, which synthesises a
    single-chunk stream for them.
    """

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        trace_id: str,
        **kwargs: Any,
    ) -> Iterator[str | BackendResult]: ...


# Retained for backwards compatibility with callers that annotate against it.
Backend = Callable[..., "str | BackendResult"]


class ChatCompletionError(ValueError):
    """Raised for malformed requests; transport adapters render as 400.

    Carries an OpenAI-shaped envelope so an adapter maps it directly:
    ``return JSONResponse(err.to_error_response(), status_code=err.status_code)``.
    """

    def __init__(
        self,
        message: str,
        *,
        type: str = "invalid_request_error",  # OpenAI envelope field name
        param: str | None = None,
        code: str | None = None,
        status_code: int = 400,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.type = type
        self.param = param
        self.code = code
        self.status_code = status_code

    def to_error_response(self) -> dict[str, Any]:
        """Render the OpenAI error envelope."""
        return {
            "error": {
                "message": self.message,
                "type": self.type,
                "param": self.param,
                "code": self.code,
            }
        }


class ChatCompletionsHandler:
    """Transport-agnostic handler for OpenAI chat completion requests."""

    def __init__(
        self,
        *,
        backend: Backend | ChatBackend,
        trace_provider: TraceProvider | None = None,
    ) -> None:
        self._backend = backend
        self._tracer = trace_provider or NullTraceProvider()

    @property
    def backend_streams(self) -> bool:
        """Whether the configured backend can emit incremental deltas."""
        return isinstance(self._backend, StreamingChatBackend)

    # -- non-streaming -----------------------------------------------------

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        """Run one completion. Returns an OpenAI ``chat.completion`` object."""
        messages, model, extra = _parse_request(request)

        trace_id = self._tracer.start_trace(
            "chat.completion", model=model, n_messages=len(messages)
        )

        result = _coerce(self._backend(messages, model=model, trace_id=trace_id, **extra))

        self._trace_output(trace_id, model, messages, result)

        message: dict[str, Any] = {"role": "assistant", "content": result.content}
        if result.tool_calls:
            message["tool_calls"] = list(result.tool_calls)

        return {
            "id": _completion_id(),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": result.finish_reason,
                }
            ],
            "usage": estimate_usage(messages, _completion_text(result)),
        }

    # -- streaming ---------------------------------------------------------

    def handle_stream(self, request: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Run one completion as OpenAI ``chat.completion.chunk`` objects.

        Emits a role delta, then content (and tool-call) deltas, then a final
        chunk carrying ``finish_reason``. Validation is eager: a malformed
        request raises :class:`ChatCompletionError` from this call, not from
        the first ``next()`` on the returned iterator.

        A backend without ``stream`` is not an error — its whole message is
        delivered as one content chunk, so a caller can always take this path.
        """
        messages, model, extra = _parse_request(request)
        return self._stream(messages, model, extra)

    def _stream(
        self,
        messages: list[dict[str, Any]],
        model: str,
        extra: dict[str, Any],
    ) -> Iterator[dict[str, Any]]:
        trace_id = self._tracer.start_trace(
            "chat.completion", model=model, n_messages=len(messages), stream=True
        )
        completion_id = _completion_id()
        created = int(time.time())

        def chunk(delta: dict[str, Any], finish_reason: str | None) -> dict[str, Any]:
            return {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }

        yield chunk({"role": "assistant"}, None)

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        truncated = False

        for piece in self._backend_pieces(messages, model, trace_id, extra):
            if piece.content:
                text_parts.append(piece.content)
                yield chunk({"content": piece.content}, None)
            if piece.tool_calls:
                start = len(tool_calls)
                tool_calls.extend(piece.tool_calls)
                yield chunk(
                    {
                        "tool_calls": [
                            {"index": start + i, **tc} for i, tc in enumerate(piece.tool_calls)
                        ]
                    },
                    None,
                )
            truncated = truncated or piece.truncated

        final = BackendResult(
            content="".join(text_parts) or None,
            tool_calls=tool_calls or None,
            truncated=truncated,
        )
        self._trace_output(trace_id, model, messages, final, streamed=True)
        yield chunk({}, final.finish_reason)

    def _backend_pieces(
        self,
        messages: list[dict[str, Any]],
        model: str,
        trace_id: str,
        extra: dict[str, Any],
    ) -> Iterator[BackendResult]:
        """Yield backend output as :class:`BackendResult` pieces.

        Capability is checked structurally up front — never by calling
        ``stream`` and catching, which would swallow a genuine backend fault.
        """
        if isinstance(self._backend, StreamingChatBackend):
            for piece in self._backend.stream(messages, model=model, trace_id=trace_id, **extra):
                yield _coerce(piece)
            return
        yield _coerce(self._backend(messages, model=model, trace_id=trace_id, **extra))

    # -- shared ------------------------------------------------------------

    def _trace_output(
        self,
        trace_id: str,
        model: str,
        messages: list[dict[str, Any]],
        result: BackendResult,
        **metadata: Any,
    ) -> None:
        self._tracer.log_generation(
            trace_id,
            model=model,
            prompt=messages,
            output=result.text,
            finish_reason=result.finish_reason,
            **metadata,
        )
        self._tracer.flush()


def render_sse(chunks: Iterable[dict[str, Any]]) -> Iterator[str]:
    """Render completion chunks as Server-Sent Events lines.

    Each chunk becomes ``data: {json}\\n\\n``; the stream is terminated by the
    OpenAI sentinel ``data: [DONE]\\n\\n``.
    """
    for chunk in chunks:
        yield f"data: {json.dumps(chunk)}\n\n"
    yield "data: [DONE]\n\n"


def estimate_usage(messages: list[dict[str, Any]], completion: str) -> dict[str, int]:
    """Approximate OpenAI ``usage`` counts by whitespace word count.

    This is the single seam for token accounting. It is deliberately a rough
    approximation — swap the body for a real tokenizer (e.g. the served model's
    own) and every usage report in the serving path becomes exact; no call site
    changes. Counts floor at 1 so a client never sees a zero-token turn.
    """
    prompt_tokens = sum(len(str(m.get("content", "")).split()) for m in messages)
    completion_tokens = len(completion.split())
    return {
        "prompt_tokens": max(prompt_tokens, 1),
        "completion_tokens": max(completion_tokens, 1),
        "total_tokens": max(prompt_tokens, 1) + max(completion_tokens, 1),
    }


def _parse_request(
    request: dict[str, Any],
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """Validate the request and split it into (messages, model, backend kwargs)."""
    messages = request.get("messages")
    if messages is None:
        raise ChatCompletionError("request missing 'messages'", param="messages")
    if not isinstance(messages, list) or len(messages) == 0:
        raise ChatCompletionError("'messages' must have at least one entry", param="messages")

    model = request.get("model", "axiom-default")
    extra = {k: request[k] for k in _FORWARDED_REQUEST_FIELDS if k in request}
    return messages, model, extra


def _coerce(output: str | BackendResult) -> BackendResult:
    """Normalise a backend return value to a :class:`BackendResult`."""
    if isinstance(output, BackendResult):
        return output
    if isinstance(output, str):
        return BackendResult(content=output)
    raise TypeError(f"backend returned {type(output).__name__}; expected str or BackendResult")


def _completion_text(result: BackendResult) -> str:
    """Text used for usage accounting — content plus any tool-call payload."""
    if not result.tool_calls:
        return result.text
    return " ".join([result.text, json.dumps(list(result.tool_calls))]).strip()


def _completion_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


__all__ = [
    "Backend",
    "BackendResult",
    "ChatBackend",
    "ChatCompletionError",
    "ChatCompletionsHandler",
    "StreamingChatBackend",
    "estimate_usage",
    "render_sse",
]
