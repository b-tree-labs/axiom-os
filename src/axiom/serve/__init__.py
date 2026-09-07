# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Axiom serve: transport-agnostic HTTP-shaped handlers.

OpenAI-compatible chat completion is a pure dict → dict handler (dict →
iterator-of-dict when streaming) so that any transport (FastAPI, Starlette,
a unit test) can drive it. This is the seam between Open WebUI / any OpenAI
client and Axiom's internal engines (research loop, direct LLM, evals).

Nothing here mounts a router or imports a web framework: an adapter owns
request/response marshaling, this package owns the wire contract.

Slice 4 of Phase 0.
"""

from __future__ import annotations

from axiom.serve.chat_completions import (
    Backend,
    BackendResult,
    ChatBackend,
    ChatCompletionError,
    ChatCompletionsHandler,
    StreamingChatBackend,
    estimate_usage,
    render_sse,
)

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
