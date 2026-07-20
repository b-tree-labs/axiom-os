# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Axiom serve: transport-agnostic HTTP-shaped handlers.

OpenAI-compatible chat completion is a pure dict → dict handler so that
any transport (FastAPI, Starlette, a unit test) can drive it. This is
the seam between Open WebUI / any OpenAI client and Axiom's internal
engines (research loop, direct LLM, evals).

Slice 4 of Phase 0.
"""

from __future__ import annotations

from axiom.serve.chat_completions import (
    ChatCompletionError,
    ChatCompletionsHandler,
)

__all__ = ["ChatCompletionError", "ChatCompletionsHandler"]
